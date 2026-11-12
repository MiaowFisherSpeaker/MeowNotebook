//! SSH 本地端口转发,相当于 `ssh -L 本地端口:远端主机:远端端口 user@host`,可以同时转发多个端口。
//! 只用 russh(纯 Rust),不依赖系统里的 ssh 命令,所以密码登录也能用。
//! 服务器指纹:第一次连接时交给界面让用户确认,之后不一致就拒绝(防中间人)。
use russh::client::{self, Handle};
use russh::keys::{self, HashAlg, PrivateKeyWithHashAlg, PublicKeyOrCertificate};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::Mutex;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Auth {
    Password { password: String },
    /// path 为空:依次试 ~/.ssh/id_ed25519、id_ecdsa、id_rsa(和 ssh 命令的默认行为一样)
    Key { path: String, passphrase: Option<String> },
}

#[derive(Clone, Debug)]
pub struct Target {
    pub host: String,
    pub port: u16,
    pub user: String,
    pub auth: Auth,
    /// 已确认过的服务器指纹(SHA256:…);None = 第一次连接
    pub fingerprint: Option<String>,
}

/// 一条转发规则:本机 127.0.0.1:local → (经服务器)remote_host:remote_port
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Forward {
    pub local: u16,
    pub remote_host: String,
    pub remote: u16,
}

#[derive(Debug, Serialize)]
#[serde(tag = "code", rename_all = "snake_case")]
pub enum TunnelError {
    /// 第一次连接:请用户确认指纹后带上它重连
    UnknownHost { fingerprint: String },
    /// 指纹和上次确认的不一致
    HostKeyChanged { expected: String, got: String },
    Auth { message: String },
    Connect { message: String },
    LocalPort { message: String },
}

impl std::fmt::Display for TunnelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnknownHost { fingerprint } => write!(f, "第一次连接这台服务器,指纹 {fingerprint}"),
            Self::HostKeyChanged { expected, got } => write!(f, "服务器指纹变了:之前 {expected},现在 {got}"),
            Self::Auth { message } | Self::Connect { message } | Self::LocalPort { message } => f.write_str(message),
        }
    }
}

struct Client {
    expected: Option<String>,
    seen: Arc<StdMutex<Option<String>>>,
}

fn fingerprint(k: &PublicKeyOrCertificate) -> String {
    match k {
        PublicKeyOrCertificate::PublicKey { key, .. } => key.fingerprint(HashAlg::Sha256).to_string(),
        PublicKeyOrCertificate::Certificate(c) => c.public_key().fingerprint(HashAlg::Sha256).to_string(),
    }
}

impl client::Handler for Client {
    type Error = russh::Error;

    async fn check_server_key(&mut self, key: &PublicKeyOrCertificate) -> Result<bool, Self::Error> {
        let fp = fingerprint(key);
        *self.seen.lock().unwrap() = Some(fp.clone());
        Ok(self.expected.as_deref() == Some(fp.as_str()))
    }
}

pub fn home() -> Option<PathBuf> {
    std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")).map(PathBuf::from)
}

pub fn expand_home(p: &str) -> String {
    match p.strip_prefix('~') {
        Some(rest) => home().map(|h| format!("{}{rest}", h.display())).unwrap_or_else(|| p.to_string()),
        None => p.to_string(),
    }
}

/// 私钥文件:填了就用它;没填就找 ssh 默认的那几个
fn key_candidates(path: &str) -> Vec<String> {
    if !path.trim().is_empty() {
        return vec![expand_home(path.trim())];
    }
    ["id_ed25519", "id_ecdsa", "id_rsa"]
        .iter()
        .filter_map(|n| home().map(|h| h.join(".ssh").join(n)))
        .filter(|p| p.is_file())
        .map(|p| p.display().to_string())
        .collect()
}

async fn open(t: &Target) -> Result<Handle<Client>, TunnelError> {
    let config = Arc::new(client::Config {
        inactivity_timeout: None,
        keepalive_interval: Some(Duration::from_secs(20)),
        keepalive_max: 3,
        // 关掉 Nagle:终端按键、网页小请求都是小包,不关的话每个包可能多等几十毫秒
        nodelay: true,
        ..Default::default()
    });
    let seen = Arc::new(StdMutex::new(None));
    let handler = Client { expected: t.fingerprint.clone(), seen: seen.clone() };
    let conn = tokio::time::timeout(Duration::from_secs(15), client::connect(config, (t.host.as_str(), t.port), handler)).await;
    let mut h = match conn {
        Err(_) => return Err(TunnelError::Connect { message: format!("连接 {}:{} 超时", t.host, t.port) }),
        Ok(Ok(h)) => h,
        Ok(Err(e)) => {
            let got = seen.lock().unwrap().clone();
            return Err(match (got, &t.fingerprint) {
                (Some(got), None) => TunnelError::UnknownHost { fingerprint: got },
                (Some(got), Some(exp)) if &got != exp => TunnelError::HostKeyChanged { expected: exp.clone(), got },
                _ => TunnelError::Connect { message: format!("连接 {}:{} 失败:{e}", t.host, t.port) },
            });
        }
    };
    let auth_err = |e: russh::Error| TunnelError::Auth { message: format!("登录失败:{e}") };
    match &t.auth {
        Auth::Password { password } => {
            if h.authenticate_password(&t.user, password).await.map_err(auth_err)?.success() {
                return Ok(h);
            }
            Err(TunnelError::Auth { message: "用户名或密码不对".into() })
        }
        Auth::Key { path, passphrase } => {
            let cands = key_candidates(path);
            if cands.is_empty() {
                return Err(TunnelError::Auth { message: "没找到私钥:~/.ssh 下没有 id_ed25519 / id_ecdsa / id_rsa,请填私钥文件路径".into() });
            }
            let mut last = String::new();
            for p in &cands {
                let key = match keys::load_secret_key(p, passphrase.as_deref().filter(|s| !s.is_empty())) {
                    Ok(k) => k,
                    Err(e) => {
                        last = format!("读取私钥 {p} 失败:{e}");
                        continue;
                    }
                };
                let hash = h.best_supported_rsa_hash().await.map_err(auth_err)?.flatten();
                if h.authenticate_publickey(&t.user, PrivateKeyWithHashAlg::new(Arc::new(key), hash)).await.map_err(auth_err)?.success() {
                    return Ok(h);
                }
                last = format!("服务器不接受私钥 {p}");
            }
            Err(TunnelError::Auth { message: last })
        }
    }
}

/// SSH 连接:多个转发共用一条;断了(睡眠唤醒、换网络)由下一个本地连接触发重连
struct Session {
    target: Target,
    cur: StdMutex<Option<Arc<Handle<Client>>>>,
    reconnect: Mutex<()>,
}

impl Session {
    async fn handle(&self) -> Option<Arc<Handle<Client>>> {
        if let Some(h) = self.cur.lock().unwrap().clone().filter(|h| !h.is_closed()) {
            return Some(h);
        }
        let _g = self.reconnect.lock().await; // 同一时间只重连一次
        if let Some(h) = self.cur.lock().unwrap().clone().filter(|h| !h.is_closed()) {
            return Some(h);
        }
        let h = Arc::new(open(&self.target).await.ok()?);
        *self.cur.lock().unwrap() = Some(h.clone());
        Some(h)
    }

    fn drop_if(&self, h: &Arc<Handle<Client>>) {
        let mut g = self.cur.lock().unwrap();
        if g.as_ref().is_some_and(|c| Arc::ptr_eq(c, h)) {
            *g = None;
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ForwardStatus {
    pub local: u16,
    pub remote_host: String,
    pub remote: u16,
    /// 本地端口没监听成功时的原因
    pub error: Option<String>,
    /// 当前经这条转发的连接数
    pub active: usize,
}

struct Listening {
    rule: Forward,
    error: Option<String>,
    active: Arc<AtomicUsize>,
}

/// 运行中的隧道。close() 或 drop 后本地端口释放、SSH 断开。
pub struct Tunnel {
    session: Arc<Session>,
    tasks: Vec<tokio::task::JoinHandle<()>>,
    forwards: Vec<Listening>,
}

impl Tunnel {
    pub fn status(&self) -> Vec<ForwardStatus> {
        self.forwards
            .iter()
            .map(|f| ForwardStatus {
                local: f.rule.local,
                remote_host: f.rule.remote_host.clone(),
                remote: f.rule.remote,
                error: f.error.clone(),
                active: f.active.load(Ordering::Relaxed),
            })
            .collect()
    }

    pub async fn close(mut self) {
        for t in self.tasks.drain(..) {
            t.abort();
        }
        let h = self.session.cur.lock().unwrap().take();
        if let Some(h) = h {
            let _ = tokio::time::timeout(Duration::from_secs(2), h.disconnect(russh::Disconnect::ByApplication, "", "zh")).await;
        }
    }
}

impl Drop for Tunnel {
    fn drop(&mut self) {
        for t in &self.tasks {
            t.abort();
        }
    }
}

/// 登录并开始转发。required = 必须监听成功的那条(网页),它的端口被占用就整体失败;其余的失败只记在状态里。
pub async fn start(t: Target, rules: Vec<Forward>, required: usize) -> Result<Tunnel, TunnelError> {
    let first = open(&t).await?;
    let session = Arc::new(Session { target: t, cur: StdMutex::new(Some(Arc::new(first))), reconnect: Mutex::new(()) });
    let mut tasks = vec![];
    let mut forwards = vec![];
    for (i, rule) in rules.into_iter().enumerate() {
        let active = Arc::new(AtomicUsize::new(0));
        match TcpListener::bind(("127.0.0.1", rule.local)).await {
            Ok(l) => {
                tasks.push(tokio::spawn(serve(l, rule.clone(), session.clone(), active.clone())));
                forwards.push(Listening { rule, error: None, active });
            }
            Err(e) => {
                let msg = format!("本机端口 {} 被占用(或没有权限):{e}", rule.local);
                if i == required {
                    for t in &tasks {
                        t.abort();
                    }
                    return Err(TunnelError::LocalPort { message: msg });
                }
                forwards.push(Listening { rule, error: Some(msg), active });
            }
        }
    }
    Ok(Tunnel { session, tasks, forwards })
}

async fn serve(listener: TcpListener, rule: Forward, session: Arc<Session>, active: Arc<AtomicUsize>) {
    loop {
        let Ok((mut sock, peer)) = listener.accept().await else { continue };
        let _ = sock.set_nodelay(true);
        let (session, rule, active) = (session.clone(), rule.clone(), active.clone());
        // 每个本地连接各开一个 SSH 通道,互不等待(浏览器会同时发很多请求)
        tokio::spawn(async move {
            let mut ch = None;
            for _ in 0..2 {
                let Some(h) = session.handle().await else { break };
                match h.channel_open_direct_tcpip(rule.remote_host.clone(), rule.remote as u32, peer.ip().to_string(), peer.port() as u32).await {
                    Ok(c) => {
                        ch = Some(c);
                        break;
                    }
                    Err(_) if h.is_closed() => session.drop_if(&h), // 连接断了:重连再试一次
                    Err(_) => break,                               // 服务器那边这个端口没人监听
                }
            }
            let Some(ch) = ch else { return };
            active.fetch_add(1, Ordering::Relaxed);
            let mut stream = ch.into_stream();
            let _ = tokio::io::copy_bidirectional(&mut sock, &mut stream).await;
            active.fetch_sub(1, Ordering::Relaxed);
        });
    }
}
