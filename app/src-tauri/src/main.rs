// Windows 正式包不弹出控制台窗口
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    meownotebook_lib::run()
}
