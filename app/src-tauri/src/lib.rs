//! MeowNotebook 桌面客户端:一个很薄的壳。
//! 登录页(ui/index.html)填 SSH 信息 → 这里建隧道把服务器的 8765 转发到本机 → 窗口直接打开那个网站。
//! 网页由服务器提供,网页端更新不需要改应用;应用只负责连接和自身更新。
mod tunnel;

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::menu::{CheckMenuItem, CheckMenuItemBuilder, MenuBuilder, MenuItemBuilder, SubmenuBuilder};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Manager, RunEvent, State, Url, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_updater::UpdaterExt;

const KEYRING_SERVICE: &str = "MeowNotebook";
const DEFAULT_LOCAL_PORT: u16 = 18765;

#[derive(Clone, Debug, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct Profile {
    pub id: String,
    pub name: String,
    /// "ssh" 或 "direct"(网站就在本机 / 已有别的转发,直接填网址)
    pub kind: String,
    pub host: String,
    pub port: u16,
    pub user: String,
    /// "password" 或 "key"
    pub auth: String,
    pub key_path: String,
    pub remote_port: u16,
    pub local_port: u16,
    pub fingerprint: Option<String>,
    /// 密码 / 私钥口令存进系统钥匙串(macOS 钥匙串、Windows 凭据管理器),不写进配置文件
    pub remember: bool,
    pub url: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub profiles: Vec<Profile>,
    pub last: Option<String>,
    pub auto_connect: bool,
    /// 应用自动更新:默认关,发现新版本时询问
    pub auto_update: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self { profiles: vec![], last: None, auto_connect: true, auto_update: false }
    }
}

struct AppState {
    config: Mutex<Config>,
    tunnel: tokio::sync::Mutex<Option<tunnel::Tunnel>>,
    connected: Mutex<Option<String>>,
    login_url: Mutex<Option<Url>>,
}

fn config_path(app: &AppHandle) -> PathBuf {
    app.path().app_config_dir().unwrap_or_else(|_| PathBuf::from(".")).join("config.json")
}

fn load_config(app: &AppHandle) -> Config {
    std::fs::read_to_string(config_path(app)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

fn save_config(app: &AppHandle, c: &Config) -> Result<(), String> {
    let p = config_path(app);
    if let Some(d) = p.parent() {
        std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
    }
    std::fs::write(&p, serde_json::to_string_pretty(c).unwrap()).map_err(|e| e.to_string())
}

fn keyring_entry(id: &str) -> Option<keyring::Entry> {
    keyring::Entry::new(KEYRING_SERVICE, id).ok()
}

fn channel(app: &AppHandle) -> &'static str {
    if app.config().identifier.ends_with(".dev") { "dev" } else { "stable" }
}

#[derive(Serialize)]
struct StateView {
    profiles: Vec<Profile>,
    last: Option<String>,
    auto_connect: bool,
    auto_update: bool,
    version: String,
    channel: &'static str,
    has_secret: Vec<String>,
}

#[tauri::command]
fn get_state(app: AppHandle, st: State<'_, AppState>) -> StateView {
    let c = st.config.lock().unwrap().clone();
    let has_secret = c
        .profiles
        .iter()
        .filter(|p| p.remember && keyring_entry(&p.id).and_then(|e| e.get_password().ok()).is_some())
        .map(|p| p.id.clone())
        .collect();
    StateView {
        profiles: c.profiles,
        last: c.last,
        auto_connect: c.auto_connect,
        auto_update: c.auto_update,
        version: app.package_info().version.to_string(),
        channel: channel(&app),
        has_secret,
    }
}

#[tauri::command]
fn save_profile(app: AppHandle, st: State<'_, AppState>, mut profile: Profile) -> Result<Profile, String> {
    let mut c = st.config.lock().unwrap();
    if profile.id.is_empty() {
        profile.id = format!("p{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis());
    }
    if let Some(old) = c.profiles.iter().find(|p| p.id == profile.id) {
        // 主机变了,旧指纹不再适用
        if old.host != profile.host || old.port != profile.port {
            profile.fingerprint = None;
        } else if profile.fingerprint.is_none() {
            profile.fingerprint = old.fingerprint.clone();
        }
    }
    if !profile.remember {
        if let Some(e) = keyring_entry(&profile.id) {
            let _ = e.delete_credential();
        }
    }
    match c.profiles.iter_mut().find(|p| p.id == profile.id) {
        Some(p) => *p = profile.clone(),
        None => c.profiles.push(profile.clone()),
    }
    save_config(&app, &c)?;
    Ok(profile)
}

#[tauri::command]
fn delete_profile(app: AppHandle, st: State<'_, AppState>, id: String) -> Result<(), String> {
    let mut c = st.config.lock().unwrap();
    c.profiles.retain(|p| p.id != id);
    if c.last.as_deref() == Some(id.as_str()) {
        c.last = None;
    }
    if let Some(e) = keyring_entry(&id) {
        let _ = e.delete_credential();
    }
    save_config(&app, &c)
}

#[tauri::command]
fn set_options(app: AppHandle, st: State<'_, AppState>, auto_connect: Option<bool>, auto_update: Option<bool>) -> Result<(), String> {
    let mut c = st.config.lock().unwrap();
    if let Some(v) = auto_connect {
        c.auto_connect = v;
    }
    if let Some(v) = auto_update {
        c.auto_update = v;
        let _ = app.state::<AutoItem>().0.set_checked(v);
    }
    save_config(&app, &c)
}

/// 连接。成功返回要打开的网址;失败返回 {code, message, ...},code = need_secret / unknown_host / host_key_changed / auth / connect / local_port
#[tauri::command]
async fn connect(
    app: AppHandle,
    st: State<'_, AppState>,
    id: String,
    secret: Option<String>,
    trust: Option<String>,
) -> Result<String, serde_json::Value> {
    let err = |code: &str, message: String| serde_json::json!({ "code": code, "message": message });
    let mut p = st.config.lock().unwrap().profiles.iter().find(|p| p.id == id).cloned()
        .ok_or_else(|| err("connect", "找不到这个连接".into()))?;
    let version = app.package_info().version.to_string();
    let query = format!("shell=desktop&v={version}&ch={}", channel(&app));

    if let Some(old) = st.tunnel.lock().await.take() {
        old.close().await;
    }
    *st.connected.lock().unwrap() = None;

    if p.kind == "direct" {
        let mut u = Url::parse(p.url.trim()).map_err(|e| err("connect", format!("网址不对:{e}")))?;
        u.set_query(Some(&query));
        remember_last(&app, &st, &id);
        *st.connected.lock().unwrap() = Some(id);
        return Ok(u.to_string());
    }

    let secret = secret.filter(|s| !s.is_empty()).or_else(|| {
        if p.remember { keyring_entry(&p.id).and_then(|e| e.get_password().ok()) } else { None }
    });
    let auth = if p.auth == "key" {
        tunnel::Auth::Key { path: p.key_path.clone(), passphrase: secret.clone() }
    } else {
        match secret.clone() {
            Some(password) => tunnel::Auth::Password { password },
            None => return Err(err("need_secret", "请输入密码".into())),
        }
    };
    if let Some(fp) = trust {
        p.fingerprint = Some(fp);
    }
    let target = tunnel::Target {
        host: p.host.trim().to_string(),
        port: if p.port == 0 { 22 } else { p.port },
        user: p.user.trim().to_string(),
        auth,
        remote_port: if p.remote_port == 0 { 8765 } else { p.remote_port },
        fingerprint: p.fingerprint.clone(),
    };
    let want = if p.local_port == 0 { DEFAULT_LOCAL_PORT } else { p.local_port };
    let t = match tunnel::start(target.clone(), want).await {
        // 端口被占用(比如另开了一个客户端):换一个空闲端口。网页的本地设置按端口区分,所以优先用固定端口
        Err(tunnel::TunnelError::LocalPort { .. }) => tunnel::start(target, 0).await,
        r => r,
    }
    .map_err(|e| {
        let mut v = serde_json::to_value(&e).unwrap();
        v["message"] = serde_json::Value::String(e.to_string());
        v
    })?;

    // 连上了才保存:确认过的指纹、记住的密码
    {
        let mut c = st.config.lock().unwrap();
        if let Some(q) = c.profiles.iter_mut().find(|q| q.id == id) {
            q.fingerprint = p.fingerprint.clone();
        }
        let _ = save_config(&app, &c);
    }
    if p.remember {
        if let (Some(s), Some(e)) = (secret.as_ref(), keyring_entry(&p.id)) {
            let _ = e.set_password(s);
        }
    }
    remember_last(&app, &st, &id);
    let url = format!("http://localhost:{}/?{query}", t.local_port);
    *st.tunnel.lock().await = Some(t);
    *st.connected.lock().unwrap() = Some(id);
    Ok(url)
}

fn remember_last(app: &AppHandle, st: &State<'_, AppState>, id: &str) {
    let mut c = st.config.lock().unwrap();
    c.last = Some(id.to_string());
    let _ = save_config(app, &c);
}

/// 回到登录页(菜单"切换服务器")
fn back_to_login(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let st = app.state::<AppState>();
        if let Some(t) = st.tunnel.lock().await.take() {
            t.close().await;
        }
        *st.connected.lock().unwrap() = None;
        let login = st.login_url.lock().unwrap().clone();
        if let (Some(w), Some(mut u)) = (app.get_webview_window("main"), login) {
            u.set_query(Some("manual=1"));
            let _ = w.navigate(u);
        }
    });
}

async fn check_update(app: AppHandle, manual: bool) {
    let auto = app.state::<AppState>().config.lock().unwrap().auto_update;
    let say = |kind: MessageDialogKind, msg: String| {
        app.dialog().message(msg).title("MeowNotebook 更新").kind(kind).show(|_| {});
    };
    let update = match app.updater() {
        Ok(u) => u.check().await,
        Err(e) => {
            if manual {
                say(MessageDialogKind::Error, format!("无法检查更新:{e}"));
            }
            return;
        }
    };
    let update = match update {
        Ok(Some(u)) => u,
        Ok(None) => {
            if manual {
                say(MessageDialogKind::Info, format!("已经是最新版本({})。", app.package_info().version));
            }
            return;
        }
        Err(e) => {
            if manual {
                say(MessageDialogKind::Error, format!("检查更新失败:{e}"));
            }
            return;
        }
    };
    let install = {
        let app = app.clone();
        move |update: tauri_plugin_updater::Update| {
            tauri::async_runtime::spawn(async move {
                match update.download_and_install(|_, _| {}, || {}).await {
                    Ok(()) => app.restart(),
                    Err(e) => {
                        app.dialog().message(format!("更新失败:{e}")).title("MeowNotebook 更新").kind(MessageDialogKind::Error).show(|_| {});
                    }
                }
            });
        }
    };
    if auto && !manual {
        install(update);
        return;
    }
    let notes = update.body.clone().unwrap_or_default();
    let msg = format!(
        "发现新版本 {}(当前 {})。\n\n{}\n\n现在更新吗?更新后应用会自动重启,服务器上的会话不受影响。",
        update.version,
        update.current_version,
        notes.chars().take(600).collect::<String>()
    );
    app.dialog()
        .message(msg)
        .title("MeowNotebook 更新")
        .buttons(MessageDialogButtons::OkCancelCustom("更新".into(), "以后再说".into()))
        .show(move |yes| {
            if yes {
                install(update);
            }
        });
}

/// 菜单里"自动更新应用"那一项,登录页改了设置时同步勾选
struct AutoItem(CheckMenuItem<tauri::Wry>);

fn build_menu(app: &AppHandle, auto_update: bool) -> tauri::Result<CheckMenuItem<tauri::Wry>> {
    let switch = MenuItemBuilder::with_id("switch", "切换服务器…").accelerator("CmdOrCtrl+Shift+L").build(app)?;
    let reload = MenuItemBuilder::with_id("reload", "重新加载").accelerator("CmdOrCtrl+R").build(app)?;
    let check = MenuItemBuilder::with_id("check_update", "检查更新…").build(app)?;
    let auto = CheckMenuItemBuilder::with_id("auto_update", "自动更新应用").checked(auto_update).build(app)?;
    let main = SubmenuBuilder::new(app, "MeowNotebook")
        .item(&switch)
        .item(&reload)
        .separator()
        .item(&check)
        .item(&auto)
        .separator()
        .hide()
        .quit()
        .build()?;
    // macOS 的复制粘贴快捷键靠"编辑"菜单才生效
    let edit = SubmenuBuilder::new(app, "编辑").undo().redo().separator().cut().copy().paste().select_all().build()?;
    let view = SubmenuBuilder::new(app, "窗口").minimize().fullscreen().close_window().build()?;
    let menu = MenuBuilder::new(app).items(&[&main, &edit, &view]).build()?;
    app.set_menu(menu)?;
    Ok(auto)
}

fn dirs_downloads() -> Option<PathBuf> {
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    Some(PathBuf::from(home).join("Downloads"))
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let config = load_config(&handle);
            let auto_item = build_menu(&handle, config.auto_update)?;
            app.manage(AutoItem(auto_item));
            // 窗口在这里建:要接管"新标签页打开"和下载,这两件事网页在普通浏览器里自己能做,在应用窗口里不行
            let conf = app.config().app.windows.iter().find(|w| w.label == "main").cloned().expect("缺少 main 窗口配置");
            let opener = handle.clone();
            let win = WebviewWindowBuilder::from_config(&handle, &conf)?
                // 新标签页 / 外部链接 → 系统浏览器(隧道监听在本机,系统浏览器也能打开 localhost 的网址)
                .on_new_window(move |url, _| {
                    let _ = opener.opener().open_url(url.as_str(), None::<&str>);
                    NewWindowResponse::Deny
                })
                .on_download(|_, ev| {
                    if let DownloadEvent::Requested { url, destination } = ev {
                        let name = destination
                            .file_name()
                            .map(|n| n.to_owned())
                            .or_else(|| url.path_segments().and_then(|s| s.last()).map(|s| s.into()))
                            .unwrap_or_else(|| "download".into());
                        if let Some(dir) = dirs_downloads() {
                            *destination = dir.join(name);
                        }
                    }
                    true
                })
                .build()?;
            let login = win.url().ok();
            app.manage(AppState {
                config: Mutex::new(config),
                tunnel: tokio::sync::Mutex::new(None),
                connected: Mutex::new(None),
                login_url: Mutex::new(login),
            });
            // 启动后稍等再查更新,不和第一次连接抢时间
            let h = handle.clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(8)).await;
                check_update(h, false).await;
            });
            Ok(())
        })
        .on_menu_event(|app, ev| match ev.id().as_ref() {
            "switch" => back_to_login(app),
            "reload" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.eval("location.reload()");
                }
            }
            "check_update" => {
                tauri::async_runtime::spawn(check_update(app.clone(), true));
            }
            "auto_update" => {
                let st = app.state::<AppState>();
                let mut c = st.config.lock().unwrap();
                c.auto_update = !c.auto_update;
                let _ = save_config(app, &c);
            }
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![get_state, save_profile, delete_profile, set_options, connect])
        .build(tauri::generate_context!())
        .expect("启动 MeowNotebook 失败")
        .run(|app, ev| {
            if let RunEvent::Exit = ev {
                let st = app.state::<AppState>();
                if let Ok(mut t) = st.tunnel.try_lock() {
                    t.take();
                };
            }
        });
}
