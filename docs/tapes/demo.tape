# Render with: vhs docs/tapes/demo.tape  (from the repo root, after `uv sync`)
Output docs/assets/demo.gif
Set Shell "bash"
Set FontSize 15
Set FontFamily "Menlo"
Set Width 1200
Set Height 420
Set Padding 24
Set Theme { "name": "commitarena", "background": "#0d0d10", "foreground": "#f4f5f8", "cursor": "#88a9ff", "selection": "#28282e", "black": "#0d0d10", "brightBlack": "#5c5d66", "red": "#f66d67", "brightRed": "#f66d67", "green": "#5fd37f", "brightGreen": "#5fd37f", "yellow": "#f7b83d", "brightYellow": "#f7b83d", "blue": "#88a9ff", "brightBlue": "#88a9ff", "magenta": "#cc5ea6", "brightMagenta": "#cc5ea6", "cyan": "#88a9ff", "brightCyan": "#88a9ff", "white": "#f4f5f8", "brightWhite": "#ffffff" }
Hide
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1 && source .venv/bin/activate && export PS1='$ ' && clear"
Enter
Show
Sleep 500ms
Type "commitarena demo --no-open"
Sleep 400ms
Enter
Sleep 6s
