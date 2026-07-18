// Deploy-time surface config, loaded before app.js. The repo default (null)
// means "not the standalone web surface" — Discord / Telegram / dev behave
// exactly as before. The GitHub Pages deploy overwrites this file with
//   window.LFG_WEB = { apiBase: 'https://<public-host>/lfg' };
// which flips app.js into web-surface mode (wallet sign-in, cross-origin API).
window.LFG_WEB = null;
