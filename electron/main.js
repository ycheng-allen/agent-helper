const { app, BrowserWindow, Menu, nativeImage, Tray, shell, screen, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
const net = require('net');

let tray;
let panel;
let mainWindow;
let backend;
let port;
let quitting = false;

if (!app.requestSingleInstanceLock()) app.quit();
else app.on('second-instance', () => showMainWindow());

function availablePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const chosen = server.address().port;
      server.close(() => resolve(chosen));
    });
  });
}

function startBackend() {
  const root = app.isPackaged ? path.join(process.resourcesPath, 'app.asar.unpacked') : path.join(__dirname, '..');
  const script = path.join(root, 'agent_helper.py');
  backend = spawn(process.env.AGENT_HELPER_PYTHON || '/usr/bin/python3',
    [script, '--no-open', '--port', String(port)], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  backend.stdout.on('data', data => console.log(`[backend] ${data}`));
  backend.stderr.on('data', data => console.error(`[backend] ${data}`));
  backend.on('error', err => console.error('[backend]', err));
}

function waitForBackend() {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const check = () => {
      if (!backend || backend.exitCode !== null) return reject(new Error('数据服务未能启动'));
      const req = http.get(`http://127.0.0.1:${port}/api/data`, res => {
        res.resume();
        if (res.statusCode === 200) resolve();
        else retry();
      });
      req.on('error', retry);
    };
    const retry = () => attempts++ < 120 ? setTimeout(check, 250) : reject(new Error('数据服务启动超时'));
    check();
  });
}

function showPanel() {
  if (!port || !tray) return;
  if (!panel) {
    panel = new BrowserWindow({
      width: 460, height: 680, show: false, frame: false, resizable: false,
      skipTaskbar: true, titleBarStyle: 'hidden',
      webPreferences: { contextIsolation: true, sandbox: true }
    });
    panel.loadURL(`http://127.0.0.1:${port}`);
    panel.on('blur', () => { if (!quitting) panel.hide(); });
    panel.on('closed', () => { panel = null; });
  }
  const bounds = tray.getBounds();
  const work = screen.getDisplayNearestPoint({ x: bounds.x, y: bounds.y }).workArea;
  const x = Math.max(work.x, Math.min(Math.round(bounds.x + bounds.width / 2 - 230), work.x + work.width - 460));
  panel.setPosition(x, work.y);
  panel.show();
  panel.focus();
}

function showMainWindow() {
  if (!port) return;
  if (!mainWindow) {
    mainWindow = new BrowserWindow({
      width: 1080, height: 760, minWidth: 760, minHeight: 560,
      title: 'Agent Helper', show: false,
      webPreferences: { contextIsolation: true, sandbox: true }
    });
    mainWindow.loadURL(`http://127.0.0.1:${port}`);
    mainWindow.on('close', event => {
      if (!quitting) {
        event.preventDefault();
        mainWindow.hide();
      }
    });
    mainWindow.on('closed', () => { mainWindow = null; });
  }
  mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  const iconPath = app.isPackaged
    ? path.join(process.resourcesPath, 'app.asar.unpacked', 'electron', 'assets', 'tray-icon.png')
    : path.join(__dirname, 'assets', 'tray-icon.png');
  const icon = nativeImage.createFromPath(iconPath);
  if (icon.isEmpty()) throw new Error(`状态栏图标加载失败: ${iconPath}`);
  icon.setTemplateImage(true);
  tray = new Tray(icon);
  tray.setToolTip('Agent Helper · 点击查看面板');
  tray.on('click', () => panel?.isVisible() ? panel.hide() : showPanel());
  tray.on('right-click', () => tray.popUpContextMenu(Menu.buildFromTemplate([
    { label: '打开监控面板', click: showPanel },
    { label: '打开主窗口', click: showMainWindow },
    { label: '在浏览器中打开', click: () => shell.openExternal(`http://127.0.0.1:${port}`) },
    { type: 'separator' },
    { label: '退出', click: () => app.quit() }
  ])));
  showMainWindow();
}

app.on('activate', showMainWindow);

app.whenReady().then(async () => {
  try {
    port = await availablePort();
    startBackend();
    await waitForBackend();
    createTray();
  } catch (err) {
    dialog.showErrorBox('Agent Helper 启动失败', String(err.message || err));
    app.quit();
  }
});

app.on('before-quit', () => {
  quitting = true;
  if (backend && backend.exitCode === null) backend.kill('SIGTERM');
});
