# 更新记录

## v1.1.2 · 2026-11-12

- Strip whitespace from the updater signing key in CI

## v1.1.1 · 2026-11-12

- Fix desktop build: pass bundle args to tauri, not cargo

## v1.1.0 · 2026-11-12

- Desktop client, dockable panel layout, versioning and in-page updates
- Keep the question map in step with the reading position
- Restore the browser-style session tab bar
- Open session images in an in-page viewer with its own URL
- Cut idle browser work and add a power-saving switch (on by default)
- Open sessions at the latest question again when switching from another session
- Attach to forked sessions running as cc background sessions
- Let cca list sessions to pick from, with confirmed kill
- Speed up long sessions, dock the terminal on the right, and make cca follow the page
- Fix narrow-screen sidebar drawer and terminal linking for forked sessions
- Stop dropping and doubling keys typed with an IME in English mode
- Pick relevant conversations before writing a topic summary
- Fix QQ reply numbering and summary edge cases found in review
- Add a QQ bot, topic summaries, and survive bad networks
- Copy selections as Markdown from the right-click menu
- Keep session data lean and refresh live sessions incrementally
- Let terminal clients opt out of mouse reporting
- Harden file preview, WebSocket input and star writes
- Follow Claude Code session continuations
- Pin the tab bar together with the header
- Clip the terminal box so shrinking it leaves no gap
- Contain absolutely positioned content inside #main
- Fix blank scroll area below short conversations
- Add full-text session search with title / question / full-text modes

