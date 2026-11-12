//! 读 SSH 配置文件,列出里面的服务器(VS Code 的 Remote - SSH 用的就是这些文件)。
//! 来源:~/.ssh/config,以及 VS Code / Cursor 设置里 "remote.SSH.configFile" 指定的文件。
//! 支持 Host / HostName / User / Port / IdentityFile / LocalForward / Include,按 ssh 的规则"先写的生效"。
use crate::tunnel::{home, Forward};
use serde::Serialize;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Default, Serialize)]
pub struct Host {
    /// Host 后面写的名字
    pub alias: String,
    pub host: String,
    pub port: u16,
    pub user: String,
    pub key_path: String,
    pub forwards: Vec<Forward>,
    /// 导入后还需要注意的地方(比如用了跳板机)
    pub note: Option<String>,
    pub source: String,
}

struct Block {
    patterns: Vec<String>,
    opts: Vec<(String, String)>,
}

fn strip_quotes(s: &str) -> String {
    s.trim().trim_matches('"').to_string()
}

/// "Key Value" 或 "Key=Value"
fn split_kv(line: &str) -> Option<(String, String)> {
    let line = line.trim();
    if line.is_empty() || line.starts_with('#') {
        return None;
    }
    let i = line.find(|c: char| c.is_whitespace() || c == '=')?;
    let key = line[..i].to_ascii_lowercase();
    let val = line[i..].trim_start_matches(|c: char| c.is_whitespace() || c == '=').trim();
    Some((key, val.to_string()))
}

fn expand(p: &str) -> PathBuf {
    let p = strip_quotes(p);
    if let Some(rest) = p.strip_prefix("~/").or_else(|| p.strip_prefix("~\\")) {
        if let Some(h) = home() {
            return h.join(rest);
        }
    }
    PathBuf::from(p)
}

/// Include 的路径:相对路径按 ~/.ssh 算;文件名里可以有 *
fn include_paths(arg: &str) -> Vec<PathBuf> {
    let mut out = vec![];
    for one in arg.split_whitespace() {
        let mut p = expand(one);
        if p.is_relative() {
            if let Some(h) = home() {
                p = h.join(".ssh").join(p);
            }
        }
        let name = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
        if name.contains('*') || name.contains('?') {
            if let Ok(rd) = p.parent().unwrap_or(Path::new(".")).read_dir() {
                let mut v: Vec<PathBuf> = rd.flatten().map(|e| e.path()).filter(|f| f.is_file() && wild(&name, &f.file_name().unwrap().to_string_lossy())).collect();
                v.sort();
                out.extend(v);
            }
        } else {
            out.push(p);
        }
    }
    out
}

fn read_blocks(path: &Path, blocks: &mut Vec<Block>, depth: usize) {
    let Ok(text) = std::fs::read_to_string(path) else { return };
    if depth > 8 {
        return;
    }
    // 第一个 Host 之前的选项对所有主机生效
    blocks.push(Block { patterns: vec!["*".into()], opts: vec![] });
    for line in text.lines() {
        let Some((k, v)) = split_kv(line) else { continue };
        match k.as_str() {
            "host" => blocks.push(Block { patterns: v.split_whitespace().map(strip_quotes).collect(), opts: vec![] }),
            // Match 的条件太多,不去判断:这一段整个跳过
            "match" => blocks.push(Block { patterns: vec![], opts: vec![] }),
            "include" => {
                for p in include_paths(&v) {
                    read_blocks(&p, blocks, depth + 1);
                }
                // Include 之后接着属于当前这个 Host 段的选项,另起一段继承它的条件
                let pats = blocks.iter().rev().find(|b| !b.opts.is_empty() || !b.patterns.is_empty()).map(|b| b.patterns.clone()).unwrap_or_default();
                blocks.push(Block { patterns: pats, opts: vec![] });
            }
            _ => {
                if let Some(b) = blocks.last_mut() {
                    b.opts.push((k, v));
                }
            }
        }
    }
}

/// ssh 的通配符:* 和 ?
fn wild(pat: &str, s: &str) -> bool {
    let (p, s): (Vec<char>, Vec<char>) = (pat.chars().collect(), s.chars().collect());
    let (mut i, mut j, mut star, mut mark) = (0, 0, None, 0);
    while j < s.len() {
        if i < p.len() && (p[i] == '?' || p[i] == s[j]) {
            i += 1;
            j += 1;
        } else if i < p.len() && p[i] == '*' {
            star = Some(i);
            mark = j;
            i += 1;
        } else if let Some(st) = star {
            i = st + 1;
            mark += 1;
            j = mark;
        } else {
            return false;
        }
    }
    while i < p.len() && p[i] == '*' {
        i += 1;
    }
    i == p.len()
}

fn matches(patterns: &[String], alias: &str) -> bool {
    let mut hit = false;
    for p in patterns {
        if let Some(neg) = p.strip_prefix('!') {
            if wild(neg, alias) {
                return false;
            }
        } else if wild(p, alias) {
            hit = true;
        }
    }
    hit
}

/// LocalForward [地址:]本地端口 远端主机:远端端口
fn parse_forward(v: &str) -> Option<Forward> {
    let mut it = v.split_whitespace();
    let (a, b) = (it.next()?, it.next()?);
    let local: u16 = a.rsplit(':').next()?.parse().ok()?;
    let (host, port) = if let Some(rest) = b.strip_prefix('[') {
        let (h, p) = rest.split_once("]:")?;
        (h.to_string(), p)
    } else {
        let (h, p) = b.rsplit_once(':')?;
        (h.to_string(), p)
    };
    Some(Forward { local, remote_host: host, remote: port.parse().ok()? })
}

fn resolve(blocks: &[Block], alias: &str, source: &str) -> Host {
    let mut h = Host { alias: alias.into(), port: 22, source: source.into(), ..Default::default() };
    let (mut host, mut user, mut port, mut key, mut proxy) = (None, None, None, None, None);
    for b in blocks.iter().filter(|b| matches(&b.patterns, alias)) {
        for (k, v) in &b.opts {
            match k.as_str() {
                "hostname" => host = host.or_else(|| Some(strip_quotes(v).replace("%h", alias))),
                "user" => user = user.or_else(|| Some(strip_quotes(v))),
                "port" => port = port.or_else(|| v.trim().parse::<u16>().ok()),
                "identityfile" => key = key.or_else(|| Some(strip_quotes(v))),
                "proxyjump" | "proxycommand" if !v.eq_ignore_ascii_case("none") => proxy = proxy.or_else(|| Some(k.clone())),
                "localforward" => {
                    if let Some(f) = parse_forward(v) {
                        if !h.forwards.iter().any(|x| x.local == f.local) {
                            h.forwards.push(f);
                        }
                    }
                }
                _ => {}
            }
        }
    }
    h.host = host.unwrap_or_else(|| alias.into());
    h.user = user.unwrap_or_default();
    h.port = port.unwrap_or(22);
    h.key_path = key.unwrap_or_default();
    if let Some(p) = proxy {
        h.note = Some(format!("这台用了跳板机({}),客户端还不支持,导入后可能连不上", if p == "proxyjump" { "ProxyJump" } else { "ProxyCommand" }));
    }
    h
}

/// VS Code、Cursor 等编辑器设置里 remote.SSH.configFile 指定的配置文件
fn editor_config_files() -> Vec<PathBuf> {
    let mut dirs = vec![];
    if cfg!(windows) {
        if let Some(a) = std::env::var_os("APPDATA") {
            dirs.push(PathBuf::from(a));
        }
    } else if cfg!(target_os = "macos") {
        if let Some(h) = home() {
            dirs.push(h.join("Library/Application Support"));
        }
    } else if let Some(h) = home() {
        dirs.push(h.join(".config"));
    }
    let mut out = vec![];
    for d in dirs {
        for app in ["Code", "Code - Insiders", "Cursor", "VSCodium", "Windsurf"] {
            let Ok(s) = std::fs::read_to_string(d.join(app).join("User").join("settings.json")) else { continue };
            // settings.json 可以有注释,不整体解析,只找这一项
            if let Some(i) = s.find("\"remote.SSH.configFile\"") {
                let rest = &s[i + 23..];
                if let Some(v) = rest.split('"').nth(1) {
                    let v = v.replace("\\\\", "\\");
                    if !v.is_empty() {
                        out.push(expand(&v));
                    }
                }
            }
        }
    }
    out
}

pub fn discover() -> Vec<Host> {
    let mut files: Vec<PathBuf> = editor_config_files();
    if let Some(h) = home() {
        files.push(h.join(".ssh").join("config"));
    }
    let mut seen_files = vec![];
    let mut out: Vec<Host> = vec![];
    for f in files {
        let key = f.canonicalize().unwrap_or(f.clone());
        if seen_files.contains(&key) || !f.is_file() {
            continue;
        }
        seen_files.push(key);
        let mut blocks = vec![];
        read_blocks(&f, &mut blocks, 0);
        let src = f.display().to_string();
        for b in &blocks {
            for p in &b.patterns {
                if p.contains('*') || p.contains('?') || p.starts_with('!') || out.iter().any(|h| &h.alias == p) {
                    continue;
                }
                out.push(resolve(&blocks, p, &src));
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_hosts() {
        let dir = std::env::temp_dir().join(format!("sshcfg-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let inc = dir.join("extra.conf");
        std::fs::write(&inc, "Host inc\n  HostName 10.0.0.9\n").unwrap();
        let cfg = dir.join("config");
        std::fs::write(
            &cfg,
            format!(
                "# 注释\nUser globaluser\n\nHost lab gpu\n    HostName 192.168.1.10\n    Port 2222\n    IdentityFile ~/.ssh/lab_key\n    LocalForward 8765 localhost:8765\n    LocalForward 127.0.0.1:8888 [::1]:8888\n\nHost jump-*\n  User ops\n\nHost jump-box\n  HostName j.example.com\n  ProxyJump bastion\n\nInclude {}\n\nHost *\n  User fallback\n  Port 22\n",
                inc.display()
            ),
        )
        .unwrap();
        let mut blocks = vec![];
        read_blocks(&cfg, &mut blocks, 0);
        let lab = resolve(&blocks, "lab", "t");
        assert_eq!((lab.host.as_str(), lab.port, lab.user.as_str()), ("192.168.1.10", 2222, "globaluser"));
        assert!(lab.key_path.ends_with("lab_key"));
        assert_eq!(lab.forwards, vec![
            Forward { local: 8765, remote_host: "localhost".into(), remote: 8765 },
            Forward { local: 8888, remote_host: "::1".into(), remote: 8888 },
        ]);
        let j = resolve(&blocks, "jump-box", "t");
        assert_eq!((j.host.as_str(), j.user.as_str()), ("j.example.com", "globaluser"));
        assert!(j.note.is_some());
        let i = resolve(&blocks, "inc", "t");
        assert_eq!((i.host.as_str(), i.port), ("10.0.0.9", 22));
        assert!(wild("jump-*", "jump-box") && !wild("jump-*", "lab"));
        std::fs::remove_dir_all(&dir).ok();
    }
}
