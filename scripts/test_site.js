// Tests of the setup command generator (site/app.js): quoting, validation
// and the generated commands. Plain Node, no dependencies:
//   node scripts/test_site.js
'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const {test} = require('node:test');

const site = require(path.resolve(__dirname, '../site/app.js'));

const URL_OK = 'https://gateway.example.com';

test('bash single quotes: a quote ends the string, is escaped, and it starts again', () => {
    assert.equal(site.shellQuote('abc'), "'abc'");
    assert.equal(site.shellQuote("a'b"), "'a'\\''b'");
    assert.equal(site.shellQuote("''"), "''\\'''\\'''");
    assert.equal(site.shellQuote('$(id) `id` $HOME "x" \\ ! *'), "'$(id) `id` $HOME \"x\" \\ ! *'");
});

test('PowerShell single quotes double every single quote, typographic ones too', () => {
    assert.equal(site.psQuote('abc'), "'abc'");
    assert.equal(site.psQuote("a'b"), "'a''b'");
    assert.equal(site.psQuote('a‘b’c‚d‛e'), "'a‘‘b’’c‚‚d‛‛e'");
    assert.equal(site.psQuote('$env:X `n "y"'), "'$env:X `n \"y\"'");
});

test('bash command', () => {
    const key = "sk-a'b$c";
    const result = site.buildCommand('bash', URL_OK, key, {});
    assert.equal(result.command,
        "curl -fsSL https://aitp.lol/setup.sh | bash -s -- --base-url 'https://gateway.example.com' --api-key 'sk-a'\\''b$c'");
    const flags = site.buildCommand('bash', URL_OK + '/', 'k', {codex: true, noStart: true, github: true});
    assert.equal(flags.command,
        "curl -fsSL https://github.com/2742zdz/AITP/releases/latest/download/setup.sh | bash -s -- " +
        "--base-url 'https://gateway.example.com' --api-key 'k' --codex --no-start");
});

test('PowerShell command', () => {
    const result = site.buildCommand('powershell', URL_OK, "k'ey", {codex: true});
    assert.equal(result.command,
        "& ([scriptblock]::Create((irm https://aitp.lol/setup.ps1))) -BaseUrl 'https://gateway.example.com' -ApiKey 'k''ey' -Codex");
    const github = site.buildCommand('powershell', URL_OK, 'k', {noStart: true, github: true});
    assert.equal(github.command,
        "& ([scriptblock]::Create((irm https://github.com/2742zdz/AITP/releases/latest/download/setup.ps1))) " +
        "-BaseUrl 'https://gateway.example.com' -ApiKey 'k' -NoStart");
});

test('cmd wraps the PowerShell line; & ^ | < > are literal inside its double quotes', () => {
    const key = "a&b^c|d<e>f'g";
    const result = site.buildCommand('cmd', URL_OK, key, {});
    assert.equal(result.command,
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "' +
        "& ([scriptblock]::Create((irm https://aitp.lol/setup.ps1))) -BaseUrl 'https://gateway.example.com' -ApiKey 'a&b^c|d<e>f''g'" +
        '"');
    // Exactly the two wrapping double quotes.
    assert.equal(result.command.split('"').length - 1, 2);
});

test('cmd refuses a key with a double quote, a percent or an exclamation sign', () => {
    for (const key of ['a"b', 'a%PATH%b', '%', '"', 'a!PATH!b', '!']) {
        assert.deepEqual(site.buildCommand('cmd', URL_OK, key, {}), {error: 'key-cmd'}, key);
        // The other shells take it.
        assert.ok(site.buildCommand('bash', URL_OK, key, {}).command, key);
        assert.ok(site.buildCommand('powershell', URL_OK, key, {}).command, key);
    }
    assert.equal(site.buildCommand('powershell', URL_OK, 'a"b%c', {}).command,
        "& ([scriptblock]::Create((irm https://aitp.lol/setup.ps1))) -BaseUrl 'https://gateway.example.com' -ApiKey 'a\"b%c'");
});

test('an empty key leaves the key option out (the installer asks for it)', () => {
    for (const key of ['', '   ']) {
        assert.equal(site.buildCommand('bash', URL_OK, key, {}).command,
            "curl -fsSL https://aitp.lol/setup.sh | bash -s -- --base-url 'https://gateway.example.com'");
        assert.equal(site.buildCommand('powershell', URL_OK, key, {codex: true}).command,
            "& ([scriptblock]::Create((irm https://aitp.lol/setup.ps1))) -BaseUrl 'https://gateway.example.com' -Codex");
        assert.ok(!site.buildCommand('cmd', URL_OK, key, {}).command.includes('ApiKey'));
    }
});

test('keys: surrounding whitespace removed, control characters refused', () => {
    assert.deepEqual(site.parseApiKey('  sk-1 \t'), {key: 'sk-1'});
    for (const key of ['a\nb', 'a\rb', 'a\u0000b', 'a\u001bb', 'a\u007fb', 'a\u0085b', 'a\u2028b', 'a\u2029b', 'a\tb']) {
        assert.deepEqual(site.parseApiKey(key), {error: 'control'}, JSON.stringify(key));
        assert.deepEqual(site.buildCommand('bash', URL_OK, key, {}), {error: 'key-control'});
    }
});

test('base URLs: http(s), no credentials, query or fragment, no trailing slash', () => {
    const ok = {
        'https://gateway.example.com': 'https://gateway.example.com',
        'https://gateway.example.com/': 'https://gateway.example.com',
        '  https://gateway.example.com/api//  ': 'https://gateway.example.com/api',
        'HTTPS://Gateway.Example.COM:8443/x': 'https://gateway.example.com:8443/x',
        'http://localhost:8080': 'http://localhost:8080',
        'http://127.0.0.1': 'http://127.0.0.1',
        'http://[::1]:9/': 'http://[::1]:9',
        'https://例子.example/': 'https://xn--fsqu00a.example',
        "https://g.example/a'b c": "https://g.example/a'b%20c",
    };
    for (const [raw, url] of Object.entries(ok)) {
        const result = site.parseBaseUrl(raw);
        assert.equal(result.url, url, raw);
        assert.deepEqual(result.warnings, [], raw);
    }
    const bad = {
        '': 'empty',
        '   ': 'empty',
        'gateway.example.com': 'invalid',
        'ftp://gateway.example.com': 'scheme',
        'javascript:alert(1)': 'scheme',
        'file:///etc/passwd': 'scheme',
        'https://user:pw@gateway.example.com': 'credentials',
        'https://user@gateway.example.com': 'credentials',
        'https://gateway.example.com/?a=1': 'query',
        'https://gateway.example.com/?': 'query',
        'https://gateway.example.com/#x': 'query',
        'https://gateway.example.com/#': 'query',
        'https://gate\nway.example.com': 'invalid',
        'https://gateway.example.com/\u0000': 'invalid',
    };
    for (const [raw, error] of Object.entries(bad)) {
        assert.deepEqual(site.parseBaseUrl(raw), {error}, JSON.stringify(raw));
    }
    assert.deepEqual(site.buildCommand('bash', 'ftp://x.example', 'k', {}), {error: 'url-scheme'});
    assert.deepEqual(site.buildCommand('bash', '', 'k', {}), {error: 'url-empty'});
});

test('base URLs: plain http to another machine and a /v1 suffix are warnings', () => {
    assert.deepEqual(site.parseBaseUrl('http://gateway.example.com'), {url: 'http://gateway.example.com', warnings: ['http']});
    assert.deepEqual(site.parseBaseUrl('https://gateway.example.com/v1/'), {url: 'https://gateway.example.com/v1', warnings: ['v1']});
    const result = site.buildCommand('bash', 'http://gateway.example.com', 'k', {});
    assert.ok(result.command.includes("--base-url 'http://gateway.example.com'"));
    assert.deepEqual(result.warnings, ['http']);
});

test('a URL that cmd cannot carry is refused for cmd only', () => {
    for (const raw of ['https://g.example/a%25b', 'https://g.example/a!PATH!b']) {
        assert.equal(site.parseBaseUrl(raw).url, raw);
        assert.deepEqual(site.buildCommand('cmd', raw, 'k', {}), {error: 'url-cmd'});
        assert.ok(site.buildCommand('powershell', raw, 'k', {}).command);
    }
});

test('the base URL from a link fragment, validated', () => {
    assert.equal(site.fragmentBaseUrl('#base_url=https://gateway.example.com/'), 'https://gateway.example.com');
    assert.equal(site.fragmentBaseUrl('#base_url=' + encodeURIComponent('https://g.example/x')), 'https://g.example/x');
    assert.equal(site.fragmentBaseUrl('#other=1&base_url=https://g.example'), 'https://g.example');
    for (const hash of ['', '#', '#x=1', '#base_url=', '#base_url=javascript:alert(1)',
        '#base_url=' + encodeURIComponent('https://u:p@g.example'), '#base_url=' + encodeURIComponent('https://g.example/?k=1')]) {
        assert.equal(site.fragmentBaseUrl(hash), null, hash);
    }
});

test('the page sends and stores nothing about the key', () => {
    const source = require('node:fs').readFileSync(path.resolve(__dirname, '../site/app.js'), 'utf8');
    for (const forbidden of ['fetch(', 'XMLHttpRequest', 'sendBeacon', 'WebSocket', 'sessionStorage', 'document.cookie', 'innerHTML', 'eval(']) {
        assert.ok(!source.includes(forbidden), forbidden);
    }
    // localStorage only ever holds the base URL and the language.
    const keys = [...source.matchAll(/localStorage\.(\w+)\(/g)].map((match) => match[1]);
    assert.deepEqual([...new Set(keys)].sort(), ['getItem', 'removeItem', 'setItem']);
    assert.ok(!/store\([^)]*api/i.test(source));
});

test('the later start command starts only what the setup configured', () => {
    assert.equal(site.startCommand(false), 'aitp --environment claude start');
    assert.equal(site.startCommand(true), 'aitp start');
});
