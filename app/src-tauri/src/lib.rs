//! MeowNotebook 桌面客户端:一个很薄的壳。
//! 登录页(ui/index.html)填 SSH 信息和端口转发(相当于 ssh -L)→ 这里建隧道 → 窗口打开转发过来的网站。
//! 网页由服务器提供,网页端更新不需要改应用;应用只负责连接、转发和自身更新。
mod sshconfig;
mod tunnel;

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::menu::{CheckMenuItem, CheckMenuItemBuilder, MenuBuilder, MenuItemBuilder, SubmenuBuilder};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Manager, RunEvent, State, Url, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_updater::UpdaterExt;
use tunnel::Forward;

const KEYRING_SERVICE: &str = "MeowNotebook";
const WEB_PORT: u16 = 8765;

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
    /// 私钥文件;空 = 用 ~/.ssh 下默认的私钥
    pub key_path: String,
    /// 端口转发(ssh -L)。第一条是网页:窗口打开 http://localhost:<它的本地端口>/
    pub forwards: Vec<Forward>,
    pub fingerprint: Option<String>,
    /// 密码 / 私钥口令存进系统钥匙串(macOS 钥匙串、Windows 凭据管理器),不写进配置文件
    pub remember: bool,
    pub url: String,
    /// 旧版本的字段,读到时转成 forwards
    #[serde(skip_serializing)]
    pub remote_port: u16,
}

impl Profile {
    fn normalize(&mut self) {
        if self.forwards.is_empty() {
            let remote = if self.remote_port == 0 { WEB_PORT } else { self.remote_port };
            self.forwards.push(Forward { local: WEB_PORT, remote_host: "127.0.0.1".into(), remote });
        }
        for f in &mut self.forwards {
            if f.remote_host.trim().is_empty() {
                f.remote_host = "127.0.0.1".into();
            }
        }
    }
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

/// 当前连接:哪个配置、打开的网址、隧道
struct Active {
    id: String,
    name: String,
    url: String,
    tunnel: Option<tunnel::Tunnel>,
}

struct AppState {
    config: Mutex<Config>,
    active: tokio::sync::Mutex<Option<Active>>,
}

/// 菜单里"自动更新应用"那一项,登录页改了设置时同步勾选
struct AutoItem(CheckMenuItem<tauri::Wry>);

/// 登录页的网址(应用自带的页面)
fn login_url(query: &str) -> Url {
    let base = if cfg!(windows) { "http://tauri.localhost/index.html" } else { "tauri://localhost/index.html" };
    let mut u = Url::parse(base).unwrap();
    if !query.is_empty() {
        u.set_query(Some(query));
    }
    u
}

fn config_path(app: &AppHandle) -> PathBuf {
    app.path().app_config_dir().unwrap_or_else(|_| PathBuf::from(".")).join("config.json")
}

fn load_config(app: &AppHandle) -> Config {
    let mut c: Config =
        std::fs::read_to_string(config_path(app)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
    for p in &mut c.profiles {
        p.normalize();
    }
    c
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
struct ActiveView {
    id: String,
    name: String,
    url: String,
    forwards: Vec<tunnel::ForwardStatus>,
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
    active: Option<ActiveView>,
}

#[tauri::command]
async fn get_state(app: AppHandle, st: State<'_, AppState>) -> Result<StateView, ()> {
    let c = st.config.lock().unwrap().clone();
    let has_secret = c
        .profiles
        .iter()
        .filter(|p| p.remember && keyring_entry(&p.id).and_then(|e| e.get_password().ok()).is_some())
        .map(|p| p.id.clone())
        .collect();
    let active = st.active.lock().await.as_ref().map(|a| ActiveView {
        id: a.id.clone(),
        name: a.name.clone(),
        url: a.url.clone(),
        forwards: a.tunnel.as_ref().map(|t| t.status()).unwrap_or_default(),
    });
    Ok(StateView {
        profiles: c.profiles,
        last: c.last,
        auto_connect: c.auto_connect,
        auto_update: c.auto_update,
        version: app.package_info().version.to_string(),
        channel: channel(&app),
        has_secret,
        active,
    })
}

#[tauri::command]
fn save_profile(app: AppHandle, st: State<'_, AppState>, mut profile: Profile) -> Result<Profile, String> {
    profile.normalize();
    let mut seen = std::collections::HashSet::new();
    for f in &profile.forwards {
        if f.local == 0 || f.remote == 0 {
            return Err("端口要在 1–65535 之间".into());
        }
        if !seen.insert(f.local) {
            return Err(format!("本机端口 {} 用了两次", f.local));
        }
    }
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

/// 读 VS Code(Remote - SSH)用的 SSH 配置:~/.ssh/config,以及 VS Code / Cursor 设置里 remote.SSH.configFile 指定的文件
#[tauri::command]
fn import_ssh_hosts() -> Vec<sshconfig::Host> {
    sshconfig::discover()
}

#[tauri::command]
async fn disconnect(st: State<'_, AppState>) -> Result<(), ()> {
    if let Some(a) = st.active.lock().await.take() {
        if let Some(t) = a.tunnel {
            t.close().await;
        }
    }
    Ok(())
}

fn web_query(app: &AppHandle, name: &str, forwards: &[Forward]) -> String {
    let fw: Vec<String> = forwards.iter().map(|f| format!("{}:{}:{}", f.local, f.remote_host, f.remote)).collect();
    let mut q = Url::parse("http://x/").unwrap();
    q.query_pairs_mut()
        .append_pair("shell", "desktop")
        .append_pair("v", &app.package_info().version.to_string())
        .append_pair("ch", channel(app))
        .append_pair("srv", name)
        .append_pair("fw", &fw.join(","))
        .append_pair("home", login_url("manual=1").as_str());
    q.query().unwrap_or_default().to_string()
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
    let found = st.config.lock().unwrap().profiles.iter().find(|p| p.id == id).cloned();
    let mut p = found.ok_or_else(|| err("connect", "找不到这个连接".into()))?;
    p.normalize();
    let name = if p.name.trim().is_empty() { p.host.clone() } else { p.name.clone() };

    // 先断开旧的:本地端口要让出来
    if let Some(a) = st.active.lock().await.take() {
        if let Some(t) = a.tunnel {
            t.close().await;
        }
    }

    if p.kind == "direct" {
        let mut u = Url::parse(p.url.trim()).map_err(|e| err("connect", format!("网址不对:{e}")))?;
        if u.scheme() == "http" {
            let (h, port) = (u.host_str().unwrap_or("localhost").to_string(), u.port_or_known_default().unwrap_or(80));
            if !probe_http(&h, port).await {
                return Err(err("connect", format!("打不开 {}:网站没在运行?(在服务器 / 本机上运行 ccweb)", p.url.trim())));
            }
        }
        u.set_query(Some(&web_query(&app, &name, &[])));
        remember_last(&app, &st, &id);
        *st.active.lock().await = Some(Active { id, name, url: u.to_string(), tunnel: None });
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
        fingerprint: p.fingerprint.clone(),
    };
    let t = tunnel::start(target, p.forwards.clone(), 0).await.map_err(|e| {
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
    let url = format!("http://localhost:{}/?{}", p.forwards[0].local, web_query(&app, &name, &p.forwards));
    let web = p.forwards[0].clone();
    *st.active.lock().await = Some(Active { id, name, url: url.clone(), tunnel: Some(t) });
    // 隧道通了,再确认服务器上的网站在运行;没运行时连接保留(其他转发照常可用),告诉用户原因
    if !probe_http("127.0.0.1", web.local).await {
        return Err(err(
            "web_down",
            format!("SSH 已连上,端口转发也在运行,但服务器上 {}:{} 没有响应:网站没在运行?在服务器上运行 ccweb 后再点\"连接\"。", web.remote_host, web.remote),
        ));
    }
    Ok(url)
}

/// 网站有没有在响应:发一个最简单的 HTTP 请求,看有没有回 "HTTP/"
async fn probe_http(host: &str, port: u16) -> bool {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let fut = async {
        let mut s = tokio::net::TcpStream::connect((host, port)).await.ok()?;
        s.write_all(b"GET /api/version HTTP/1.0\r\nHost: localhost\r\n\r\n").await.ok()?;
        let mut buf = [0u8; 5];
        s.read_exact(&mut buf).await.ok()?;
        Some(&buf == b"HTTP/")
    };
    tokio::time::timeout(std::time::Duration::from_secs(10), fut).await.ok().flatten().unwrap_or(false)
}

fn remember_last(app: &AppHandle, st: &State<'_, AppState>, id: &str) {
    let mut c = st.config.lock().unwrap();
    c.last = Some(id.to_string());
    let _ = save_config(app, &c);
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// 回到登录页(菜单 / 托盘"切换服务器")。连接保持着,登录页上可以回到网页或断开
fn back_to_login(app: &AppHandle) {
    show_main(app);
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.navigate(login_url("manual=1"));
    }
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

fn build_menu(app: &AppHandle, auto_update: bool) -> tauri::Result<CheckMenuItem<tauri::Wry>> {
    let switch = MenuItemBuilder::with_id("switch", "切换服务器 / 端口转发…").accelerator("CmdOrCtrl+Shift+L").build(app)?;
    let reload = MenuItemBuilder::with_id("reload", "重新加载").accelerator("CmdOrCtrl+R").build(app)?;
    let check = MenuItemBuilder::with_id("check_update", "检查更新…").build(app)?;
    let auto = CheckMenuItemBuilder::with_id("auto_update", "自动更新应用").checked(auto_update).build(app)?;
    let hide = MenuItemBuilder::with_id("hide", "隐藏到托盘").accelerator("CmdOrCtrl+H").build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "退出").accelerator("CmdOrCtrl+Q").build(app)?;
    let main = SubmenuBuilder::new(app, "MeowNotebook")
        .item(&switch)
        .item(&reload)
        .separator()
        .item(&check)
        .item(&auto)
        .separator()
        .item(&hide)
        .item(&quit)
        .build()?;
    // macOS 的复制粘贴快捷键靠"编辑"菜单才生效
    let edit = SubmenuBuilder::new(app, "编辑").undo().redo().separator().cut().copy().paste().select_all().build()?;
    let view = SubmenuBuilder::new(app, "窗口").minimize().fullscreen().build()?;
    let menu = MenuBuilder::new(app).items(&[&main, &edit, &view]).build()?;
    app.set_menu(menu)?;
    Ok(auto)
}

/// 托盘图标:隐藏窗口后从这里找回来;单击显示窗口
fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let show = MenuItemBuilder::with_id("show", "显示 MeowNotebook").build(app)?;
    let switch = MenuItemBuilder::with_id("switch", "切换服务器 / 端口转发…").build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "退出").build(app)?;
    let menu = MenuBuilder::new(app).item(&show).item(&switch).separator().item(&quit).build()?;
    let mut tray = TrayIconBuilder::with_id("main")
        .tooltip("MeowNotebook")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, ev| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = ev {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

fn on_menu(app: &AppHandle, id: &str) {
    match id {
        "switch" => back_to_login(app),
        "show" => show_main(app),
        "hide" => {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.hide();
            }
        }
        "quit" => app.exit(0),
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
    }
}

fn dirs_downloads() -> Option<PathBuf> {
    tunnel::home().map(|h| h.join("Downloads"))
}

pub fn run() {
    tauri::Builder::default()
        // 已经在运行时再打开:把原来的窗口显示出来(哪怕它被隐藏到了托盘)
        .plugin(tauri_plugin_single_instance::init(|app, _, _| show_main(app)))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let config = load_config(&handle);
            let auto_item = build_menu(&handle, config.auto_update)?;
            app.manage(AutoItem(auto_item));
            app.manage(AppState { config: Mutex::new(config), active: tokio::sync::Mutex::new(None) });
            build_tray(&handle)?;
            // 窗口在这里建:要接管"新标签页打开"和下载,这两件事网页在普通浏览器里自己能做,在应用窗口里不行
            let conf = app.config().app.windows.iter().find(|w| w.label == "main").cloned().expect("缺少 main 窗口配置");
            let opener = handle.clone();
            WebviewWindowBuilder::from_config(&handle, &conf)?
                // 新标签页 / 外部链接 → 系统浏览器(转发监听在本机,系统浏览器也能打开 localhost 的网址)
                .on_new_window(move |url, _| {
                    let _ = opener.opener().open_url(url.as_str(), None::<&str>);
                    NewWindowResponse::Deny
                })
                .on_download(|_, ev| {
                    if let DownloadEvent::Requested { url, destination } = ev {
                        let name = destination
                            .file_name()
                            .map(|n| n.to_owned())
                            .or_else(|| url.path_segments().and_then(|mut s| s.next_back()).map(|s| s.into()))
                            .unwrap_or_else(|| "download".into());
                        if let Some(dir) = dirs_downloads() {
                            *destination = dir.join(name);
                        }
                    }
                    true
                })
                .build()?;
            // 启动后稍等再查更新,不和第一次连接抢时间
            let h = handle.clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(8)).await;
                check_update(h, false).await;
            });
            Ok(())
        })
        .on_menu_event(|app, ev| on_menu(app, ev.id().as_ref()))
        .invoke_handler(tauri::generate_handler![
            get_state,
            save_profile,
            delete_profile,
            set_options,
            import_ssh_hosts,
            connect,
            disconnect
        ])
        .build(tauri::generate_context!())
        .expect("启动 MeowNotebook 失败")
        .run(|app, ev| match ev {
            // macOS:点 Dock 图标时把窗口找回来
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => show_main(app),
            RunEvent::Exit => {
                let st = app.state::<AppState>();
                if let Ok(mut a) = st.active.try_lock() {
                    a.take();
                };
            }
            _ => {}
        });
}
