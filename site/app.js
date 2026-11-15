// AITP setup command generator. Everything happens in this page: the API key
// is never sent anywhere, never stored and never put into the URL. The only
// thing remembered (localStorage) is the Gateway base URL and the language.
//
// The pure functions (quoting, validation, command building) are exported
// for scripts/test_site.js when this file runs under Node.
(function () {
  'use strict';

  var SITE_BASE = 'https://aitp.lol';
  var GITHUB_BASE = 'https://github.com/2742zdz/AITP/releases/latest/download';
  var REPO_URL = 'https://github.com/2742zdz/AITP';

  // --- quoting --------------------------------------------------------------

  // POSIX shell (bash, zsh): single quotes keep everything literal; a single
  // quote ends the string, is written escaped, and the string starts again.
  function shellQuote(value) {
    return "'" + value.replace(/'/g, "'\\''") + "'";
  }

  // PowerShell: in a single-quoted string only a single quote is special,
  // doubled. PowerShell also takes the typographic single quotes U+2018,
  // U+2019, U+201A and U+201B as single quotes, so they are doubled too.
  function psQuote(value) {
    return "'" + value.replace(/['\u2018\u2019\u201a\u201b]/g, '$&$&') + "'";
  }

  // cmd.exe hands the PowerShell line to powershell.exe inside "...": no
  // double quote may close it early, no %VAR% may expand, and no !VAR!
  // either (a cmd with delayed expansion on). Inside the quotes,
  // & | < > ^ are literal.
  function cmdSafe(value) {
    return !/["%!\r\n]/.test(value);
  }

  // --- validation -----------------------------------------------------------

  var LOCAL_HOSTS = ['localhost', '127.0.0.1', '[::1]'];
  // What the installers accept (setup.sh check_url, setup.ps1 Assert-Url).
  var INSTALLER_URL = /^https?:\/\/([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?(\/[^\s?#]*)?$/;

  // The Gateway base URL: { url, warnings } or { error }. `url` is the
  // serialized absolute http(s) URL without a trailing slash; non-ASCII host
  // names come out in punycode and unsafe path characters percent-encoded.
  function parseBaseUrl(raw) {
    var text = String(raw).trim();
    if (text === '') return { error: 'empty' };
    if (/[\u0000-\u001f\u007f]/.test(text)) return { error: 'invalid' };
    var url;
    try {
      url = new URL(text);
    } catch (e) {
      return { error: 'invalid' };
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return { error: 'scheme' };
    if (url.username !== '' || url.password !== '') return { error: 'credentials' };
    if (url.search !== '' || url.hash !== '' || /[?#]/.test(text)) return { error: 'query' };
    if (url.hostname === '') return { error: 'invalid' };
    var serialized = url.href.replace(/\/+$/, '');
    if (!INSTALLER_URL.test(serialized)) return { error: 'invalid' };
    var warnings = [];
    if (url.protocol === 'http:' && LOCAL_HOSTS.indexOf(url.hostname) < 0) warnings.push('http');
    if (/\/v1$/.test(serialized)) warnings.push('v1');
    return { url: serialized, warnings: warnings };
  }

  // The API key: { key } (surrounding whitespace removed; '' = ask for it)
  // or { error }.
  function parseApiKey(raw) {
    var key = String(raw).trim();
    if (/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(key)) return { error: 'control' };
    return { key: key };
  }

  // --- commands -------------------------------------------------------------

  function installerBase(github) {
    return github ? GITHUB_BASE : SITE_BASE;
  }

  function bashCommand(url, key, options) {
    var parts = ['curl -fsSL ' + installerBase(options.github) + '/setup.sh | bash -s --',
      '--base-url ' + shellQuote(url)];
    if (key !== '') parts.push('--api-key ' + shellQuote(key));
    if (options.codex) parts.push('--codex');
    if (options.noStart) parts.push('--no-start');
    return parts.join(' ');
  }

  function powershellCommand(url, key, options) {
    var parts = ['& ([scriptblock]::Create((irm ' + installerBase(options.github) + '/setup.ps1)))',
      '-BaseUrl ' + psQuote(url)];
    if (key !== '') parts.push('-ApiKey ' + psQuote(key));
    if (options.codex) parts.push('-Codex');
    if (options.noStart) parts.push('-NoStart');
    return parts.join(' ');
  }

  function cmdCommand(url, key, options) {
    return 'powershell -NoProfile -ExecutionPolicy Bypass -Command "' + powershellCommand(url, key, options) + '"';
  }

  var BUILDERS = { bash: bashCommand, powershell: powershellCommand, cmd: cmdCommand };

  // { command } or { error }: the one-line command for `shell` ('bash',
  // 'powershell' or 'cmd'). options: { codex, noStart, github }.
  function buildCommand(shell, rawUrl, rawKey, options) {
    var builder = BUILDERS[shell];
    if (!builder) return { error: 'shell' };
    var base = parseBaseUrl(rawUrl);
    if (base.error) return { error: 'url-' + base.error };
    var api = parseApiKey(rawKey);
    if (api.error) return { error: 'key-' + api.error };
    if (shell === 'cmd' && !cmdSafe(api.key)) return { error: 'key-cmd' };
    if (shell === 'cmd' && !cmdSafe(base.url)) return { error: 'url-cmd' };
    return { command: builder(base.url, api.key, options || {}), url: base.url, warnings: base.warnings };
  }

  // The base URL a link passes in the fragment (#base_url=...), validated;
  // null when there is none or it is not usable.
  function fragmentBaseUrl(hash) {
    var text = String(hash || '').replace(/^#/, '');
    if (text === '') return null;
    var value;
    try {
      value = new URLSearchParams(text).get('base_url');
    } catch (e) {
      return null;
    }
    if (value === null) return null;
    var base = parseBaseUrl(value);
    return base.error ? null : base.url;
  }

  var api = {
    shellQuote: shellQuote,
    psQuote: psQuote,
    cmdSafe: cmdSafe,
    parseBaseUrl: parseBaseUrl,
    parseApiKey: parseApiKey,
    buildCommand: buildCommand,
    fragmentBaseUrl: fragmentBaseUrl,
    SITE_BASE: SITE_BASE,
    GITHUB_BASE: GITHUB_BASE
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (typeof document === 'undefined') return;

  // --- the page -------------------------------------------------------------

  var TEXT = {
    en: {
      title: 'AITP - one-line setup',
      tagline: 'A local proxy for Claude Code and Codex.',
      heading: 'Generate your one-line setup command',
      intro: 'Enter your Gateway and API key; AITP is installed from its latest signed release and configured in one step.',
      baseUrl: 'Gateway base URL',
      baseUrlHint: 'The address your Gateway gives you, e.g. https://gateway.example.com',
      apiKey: 'API key',
      apiKeyHint: 'Optional. Leave it empty and the installer asks for it without echo, so it stays out of your shell history.',
      show: 'Show',
      hide: 'Hide',
      showKey: 'Show the API key',
      hideKey: 'Hide the API key',
      options: 'Options',
      codex: 'Also configure Codex',
      noStart: 'Do not start the local proxy yet',
      github: 'Download the installer from GitHub instead of aitp.lol',
      shellLabel: 'Your system',
      bash: 'macOS / Linux',
      powershell: 'Windows PowerShell',
      cmd: 'Windows cmd',
      bashWhere: 'Paste into Terminal.',
      powershellWhere: 'Paste into PowerShell (Windows PowerShell 5.1 or PowerShell 7).',
      cmdWhere: 'Paste into Command Prompt (cmd.exe).',
      copy: 'Copy',
      copied: 'Copied',
      copyFailed: 'Copy failed. Press Show first (the key is masked), then select the command and copy it.',
      keyMasked: 'The key is masked here; Copy copies the full command.',
      keyPrompt: 'No key given: the installer will ask for it.',
      waiting: 'Enter the Gateway base URL to see the command.',
      fixAbove: 'Correct the field marked above to see the command.',
      'url-empty': 'Enter the Gateway base URL to see the command.',
      'url-invalid': 'This is not a valid http(s) URL.',
      'url-scheme': 'The URL must start with https:// (or http:// for this machine).',
      'url-credentials': 'The URL must not contain a user name or password.',
      'url-query': 'The URL must not contain ? or #.',
      'url-cmd': 'This URL cannot be used in cmd; use the PowerShell tab.',
      'key-control': 'The key contains a line break or control character; paste it again.',
      'key-cmd': 'This key contains ", % or !, which cmd cannot pass on safely. Use the PowerShell tab, or leave the key empty and type it when the installer asks.',
      'warn-http': 'Plain http: the API key would travel unencrypted. Use https unless the Gateway runs on this machine.',
      'warn-v1': 'The URL ends in /v1. Usually the Gateway root without /v1 is meant; check your Gateway\'s instructions.',
      afterTitle: 'Afterwards',
      afterClaude: 'Run Claude Code as usual; it now goes through the local proxy:',
      afterCodex: 'Codex goes through the proxy with:',
      afterStart: 'Start the proxy later with:',
      uninstallTitle: 'Uninstall',
      uninstallText: 'Removes the services, settings, data and the program AITP installed:',
      privacyTitle: 'Your key stays here',
      privacyText: 'This page builds the command in your browser. It sends nothing anywhere, stores no key and loads nothing from other sites.',
      trustText: 'How the installer and updates are checked',
      repo: 'Source on GitHub',
      lang: '中文',
      langLabel: 'Switch to Chinese'
    },
    zh: {
      title: 'AITP - 一键配置',
      tagline: 'Claude Code 与 Codex 的本地代理。',
      heading: '生成一键配置命令',
      intro: '填入 Gateway 地址和 API Key，一条命令即可从最新的签名发布版安装 AITP 并完成配置。',
      baseUrl: 'Gateway 地址',
      baseUrlHint: '你的 Gateway 提供的地址，例如 https://gateway.example.com',
      apiKey: 'API Key',
      apiKeyHint: '可不填。留空时安装程序会以不回显的方式询问，Key 不会留在命令历史里。',
      show: '显示',
      hide: '隐藏',
      showKey: '显示 API Key',
      hideKey: '隐藏 API Key',
      options: '选项',
      codex: '同时配置 Codex',
      noStart: '暂不启动本地代理',
      github: '从 GitHub 而不是 aitp.lol 下载安装程序',
      shellLabel: '你的系统',
      bash: 'macOS / Linux',
      powershell: 'Windows PowerShell',
      cmd: 'Windows cmd',
      bashWhere: '粘贴到“终端”中运行。',
      powershellWhere: '粘贴到 PowerShell 中运行（Windows PowerShell 5.1 或 PowerShell 7）。',
      cmdWhere: '粘贴到“命令提示符”（cmd.exe）中运行。',
      copy: '复制',
      copied: '已复制',
      copyFailed: '复制失败。请先点“显示”（Key 被遮住了），再手动选中命令复制。',
      keyMasked: '此处遮住了 Key；“复制”会复制完整命令。',
      keyPrompt: '未填 Key：安装程序会询问。',
      waiting: '填入 Gateway 地址后显示命令。',
      fixAbove: '请先修正上方标出的输入项。',
      'url-empty': '填入 Gateway 地址后显示命令。',
      'url-invalid': '这不是有效的 http(s) 地址。',
      'url-scheme': '地址须以 https:// 开头（本机可用 http://）。',
      'url-credentials': '地址中不能包含用户名或密码。',
      'url-query': '地址中不能包含 ? 或 #。',
      'url-cmd': '此地址无法用于 cmd，请改用 PowerShell 标签页。',
      'key-control': 'Key 中有换行或控制字符，请重新粘贴。',
      'key-cmd': '此 Key 含有 "、% 或 !，cmd 无法安全传递。请改用 PowerShell 标签页，或将 Key 留空，等安装程序询问时再输入。',
      'warn-http': '这是明文 http：API Key 将不加密传输。除非 Gateway 就在本机，请使用 https。',
      'warn-v1': '地址以 /v1 结尾。通常应填不带 /v1 的 Gateway 根地址，请核对 Gateway 的说明。',
      afterTitle: '之后',
      afterClaude: '照常运行 Claude Code，它已经通过本地代理连接：',
      afterCodex: 'Codex 通过代理运行：',
      afterStart: '稍后启动代理：',
      uninstallTitle: '卸载',
      uninstallText: '删除 AITP 安装的服务、设置、数据和程序：',
      privacyTitle: 'Key 只留在本页',
      privacyText: '命令在你的浏览器里生成。本页不发送任何数据、不保存 Key，也不加载其他网站的内容。',
      trustText: '安装程序和更新如何校验',
      repo: 'GitHub 源代码',
      lang: 'English',
      langLabel: '切换到英文'
    }
  };

  var SHELLS = ['bash', 'powershell', 'cmd'];
  var STORE_URL = 'aitp.base_url';
  var STORE_LANG = 'aitp.lang';

  var state = {
    lang: 'en',
    shell: 'bash',
    showKey: false
  };

  function $(id) {
    return document.getElementById(id);
  }

  function store(name, value) {
    try {
      if (value === null) window.localStorage.removeItem(name);
      else window.localStorage.setItem(name, value);
    } catch (e) { /* storage unavailable: nothing is remembered */ }
  }

  function load(name) {
    try {
      return window.localStorage.getItem(name);
    } catch (e) {
      return null;
    }
  }

  function t(name) {
    return TEXT[state.lang][name] || TEXT.en[name] || name;
  }

  function applyLanguage() {
    document.documentElement.lang = state.lang === 'zh' ? 'zh-CN' : 'en';
    document.title = t('title');
    var nodes = document.querySelectorAll('[data-i18n]');
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = t(nodes[i].getAttribute('data-i18n'));
    }
    var lang = $('lang');
    lang.textContent = t('lang');
    lang.setAttribute('aria-label', t('langLabel'));
    lang.setAttribute('lang', state.lang === 'zh' ? 'en' : 'zh-CN');
    updateKeyToggle();
    render();
  }

  function updateKeyToggle() {
    var button = $('key-toggle');
    $('api-key').type = state.showKey ? 'text' : 'password';
    button.textContent = state.showKey ? t('hide') : t('show');
    button.setAttribute('aria-pressed', state.showKey ? 'true' : 'false');
    button.setAttribute('aria-label', state.showKey ? t('hideKey') : t('showKey'));
  }

  function options() {
    return {
      codex: $('opt-codex').checked,
      noStart: $('opt-no-start').checked,
      github: $('opt-github').checked
    };
  }

  // The current command, or null with a reason shown.
  var current = null;

  function setMessage(element, name) {
    element.textContent = name ? t(name) : '';
    element.hidden = !name;
  }

  function render() {
    var rawUrl = $('base-url').value;
    var rawKey = $('api-key').value;
    var result = buildCommand(state.shell, rawUrl, rawKey, options());
    var output = $('command');
    var copy = $('copy');
    var base = parseBaseUrl(rawUrl);
    $('base-url').setAttribute('aria-invalid', base.error && base.error !== 'empty' ? 'true' : 'false');
    var keyCheck = parseApiKey(rawKey);
    $('api-key').setAttribute('aria-invalid', keyCheck.error || result.error === 'key-cmd' ? 'true' : 'false');

    setMessage($('url-error'), base.error && base.error !== 'empty' ? 'url-' + base.error : null);
    setMessage($('key-error'), keyCheck.error ? 'key-' + keyCheck.error : null);
    var warnings = base.warnings || [];
    setMessage($('url-warning'), warnings.length ? 'warn-' + warnings[0] : null);
    setMessage($('url-warning-2'), warnings.length > 1 ? 'warn-' + warnings[1] : null);

    $('shell-where').textContent = t(state.shell + 'Where');
    $('after-start').hidden = !$('opt-no-start').checked;
    $('after-codex').hidden = !$('opt-codex').checked;

    if (result.error) {
      current = null;
      output.textContent = '';
      output.parentNode.hidden = true;
      copy.disabled = true;
      // A field error is shown at its field; cmd's limits only here.
      var reason = result.error === 'url-empty' ? 'waiting'
        : result.error === 'key-cmd' || result.error === 'url-cmd' ? result.error
        : 'fixAbove';
      setMessage($('command-status'), reason);
      $('command-status').className = result.error === 'url-empty' ? 'status' : 'status error';
      setMessage($('key-note'), null);
      return;
    }
    current = result.command;
    var key = keyCheck.key;
    var shown = current;
    if (key !== '' && !state.showKey) {
      // Built again with a mask in place of the key (same quoting rules).
      shown = buildCommand(state.shell, rawUrl, '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022', options()).command;
    }
    output.textContent = shown;
    output.parentNode.hidden = false;
    copy.disabled = false;
    setMessage($('command-status'), null);
    setMessage($('key-note'), key === '' ? 'keyPrompt' : state.showKey ? null : 'keyMasked');
  }

  function selectShell(shell, focus) {
    state.shell = shell;
    for (var i = 0; i < SHELLS.length; i++) {
      var tab = $('tab-' + SHELLS[i]);
      var selected = SHELLS[i] === shell;
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    }
    $('panel').setAttribute('aria-labelledby', 'tab-' + shell);
    render();
  }

  function flash(button, status, name, ok) {
    status.textContent = t(name);
    status.className = ok ? 'copy-status' : 'copy-status error';
    if (ok) {
      button.classList.add('done');
      window.setTimeout(function () {
        button.classList.remove('done');
        status.textContent = '';
      }, 2000);
    }
  }

  function copyText(text, button, status) {
    if (!text) return;
    var done = function () { flash(button, status, 'copied', true); };
    var fail = function () { flash(button, status, 'copyFailed', false); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fail);
    } else {
      fail();
    }
  }

  function defaultShell() {
    var platform = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent || '';
    return /win/i.test(platform) && !/darwin/i.test(platform) ? 'powershell' : 'bash';
  }

  function init() {
    var lang = load(STORE_LANG);
    if (lang !== 'en' && lang !== 'zh') {
      var languages = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || 'en'];
      lang = /^zh\b/i.test(languages[0]) ? 'zh' : 'en';
    }
    state.lang = lang;

    var fromLink = fragmentBaseUrl(window.location.hash);
    var remembered = load(STORE_URL);
    if (fromLink) $('base-url').value = fromLink;
    else if (remembered && !parseBaseUrl(remembered).error) $('base-url').value = remembered;

    $('base-url').addEventListener('input', render);
    $('base-url').addEventListener('change', function () {
      var base = parseBaseUrl($('base-url').value);
      store(STORE_URL, base.error ? null : base.url);
    });
    $('api-key').addEventListener('input', render);
    $('key-toggle').addEventListener('click', function () {
      state.showKey = !state.showKey;
      updateKeyToggle();
      render();
    });
    ['opt-codex', 'opt-no-start', 'opt-github'].forEach(function (id) {
      $(id).addEventListener('change', render);
    });
    $('lang').addEventListener('click', function () {
      state.lang = state.lang === 'zh' ? 'en' : 'zh';
      store(STORE_LANG, state.lang);
      applyLanguage();
    });
    SHELLS.forEach(function (shell, index) {
      var tab = $('tab-' + shell);
      tab.addEventListener('click', function () { selectShell(shell, false); });
      tab.addEventListener('keydown', function (event) {
        var next = null;
        if (event.key === 'ArrowRight') next = SHELLS[(index + 1) % SHELLS.length];
        else if (event.key === 'ArrowLeft') next = SHELLS[(index + SHELLS.length - 1) % SHELLS.length];
        else if (event.key === 'Home') next = SHELLS[0];
        else if (event.key === 'End') next = SHELLS[SHELLS.length - 1];
        if (next) {
          event.preventDefault();
          selectShell(next, true);
        }
      });
    });
    $('copy').addEventListener('click', function () {
      copyText(current, $('copy'), $('copy-status'));
    });
    var copyButtons = document.querySelectorAll('[data-copy]');
    for (var i = 0; i < copyButtons.length; i++) {
      (function (button) {
        button.addEventListener('click', function () {
          copyText(button.getAttribute('data-copy'), button, $(button.getAttribute('data-status')));
        });
      })(copyButtons[i]);
    }

    state.shell = defaultShell();
    applyLanguage();
    selectShell(state.shell, false);
    document.documentElement.classList.add('ready');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
