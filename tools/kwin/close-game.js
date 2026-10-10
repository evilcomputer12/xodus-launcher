// KWin script: ask the window manager to close a game's window, exactly as Alt+F4 does.
// Usage (Plasma 6):
//   id=$(qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript "$PWD/tools/kwin/close-game.js")
//   qdbus6 org.kde.KWin /Scripting/Script$id org.kde.kwin.Script.run
// Edit the match below for other games (resourceClass is the exe name under Wine).
workspace.windowList().forEach(function (w) {
    if (w.resourceClass == "love.exe" || w.caption == "Balatro") { w.closeWindow(); }
});
