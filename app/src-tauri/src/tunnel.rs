//! SSH 本地端口转发:127.0.0.1:<local_port> → (SSH) → 远端 127.0.0.1:<remote_port>。
//! 只用 russh(纯 Rust),不依赖系统里的 ssh 命令,所以密码登录也能用。
//! 服务器指纹:第一次连接时交给界面让用户确认,之后不一致就拒绝(防中间人)。
use russh::client::{self, Handle};
use russh::keys::{self, HashAlg, PrivateKeyWithHashAlg, PublicKeyOrCertificate};
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::Mutex;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Auth {
    Password { password: String },
    Key { path: String, passphrase: Option<String> },
}

#[derive(Clone, Debug)]
pub struct Target {
    pub host: String,
    pub port: u16,
    pub user: String,
    pub auth: Auth,
    pub remote_port: u16,
    /// 已确认过的服务器指纹(SHA256:…);None = 第一次连接
    pub fingerprint: Option<String>,
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

async fn open(t: &Target) -> Result<Handle<Client>, TunnelError> {
    let config = Arc::new(client::Config {
        inactivity_timeout: None,
        keepalive_interval: Some(Duration::from_secs(20)),
        keepalive_max: 3,
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
    let ok = match &t.auth {
        Auth::Password { password } => h.authenticate_password(&t.user, password).await.map_err(auth_err)?,
        Auth::Key { path, passphrase } => {
            let path = expand_home(path);
            let key = keys::load_secret_key(&path, passphrase.as_deref().filter(|p| !p.is_empty()))
                .map_err(|e| TunnelError::Auth { message: format!("读取私钥 {path} 失败:{e}") })?;
            let hash = h.best_supported_rsa_hash().await.map_err(auth_err)?.flatten();
            h.authenticate_publickey(&t.user, PrivateKeyWithHashAlg::new(Arc::new(key), hash)).await.map_err(auth_err)?
        }
    };
    if !ok.success() {
        return Err(TunnelError::Auth { message: "用户名或密码 / 私钥不对".into() });
    }
    Ok(h)
}

pub fn expand_home(p: &str) -> String {
    match p.strip_prefix("~") {
        Some(rest) => dirs_home().map(|h| format!("{h}{rest}")).unwrap_or_else(|| p.to_string()),
        None => p.to_string(),
    }
}

fn dirs_home() -> Option<String> {
    std::env::var("HOME").ok().or_else(|| std::env::var("USERPROFILE").ok())
}

/// 一条运行中的隧道。drop 或 close() 后本地端口释放、SSH 断开。
pub struct Tunnel {
    pub local_port: u16,
    task: tokio::task::JoinHandle<()>,
    handle: Arc<Mutex<Option<Handle<Client>>>>,
}

impl Tunnel {
    pub async fn close(self) {
        self.task.abort();
        if let Some(h) = self.handle.lock().await.take() {
            let _ = h.disconnect(russh::Disconnect::ByApplication, "", "zh").await;
        }
    }
}

impl Drop for Tunnel {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// 登录并开始转发。local_port = 0 时随便挑一个空闲端口。
/// 断线后(睡眠唤醒、换网络)下一个本地连接进来时自动重新登录。
pub async fn start(t: Target, local_port: u16) -> Result<Tunnel, TunnelError> {
    let first = open(&t).await?;
    let listener = TcpListener::bind(("127.0.0.1", local_port))
        .await
        .map_err(|e| TunnelError::LocalPort { message: format!("本地端口 {local_port} 被占用:{e}") })?;
    let local_port = listener.local_addr().map(|a| a.port()).unwrap_or(local_port);
    let handle = Arc::new(Mutex::new(Some(first)));
    let shared = handle.clone();
    let task = tokio::spawn(async move {
        loop {
            let Ok((sock, peer)) = listener.accept().await else { continue };
            let _ = sock.set_nodelay(true);
            let shared = shared.clone();
            let t = t.clone();
            tokio::spawn(async move {
                let ch = {
                    let mut g = shared.lock().await;
                    let mut ch = None;
                    for _ in 0..2 {
                        if g.as_ref().map_or(true, |h| h.is_closed()) {
                            *g = open(&t).await.ok();
                        }
                        let Some(h) = g.as_ref() else { break };
                        match h.channel_open_direct_tcpip("127.0.0.1", t.remote_port as u32, peer.ip().to_string(), peer.port() as u32).await {
                            Ok(c) => { ch = Some(c); break; }
                            Err(_) => *g = None, // 连接多半已断,重连一次
                        }
                    }
                    ch
                };
                if let Some(ch) = ch {
                    let mut stream = ch.into_stream();
                    let mut sock = sock;
                    let _ = tokio::io::copy_bidirectional(&mut sock, &mut stream).await;
                }
            });
        }
    });
    Ok(Tunnel { local_port, task, handle })
}
