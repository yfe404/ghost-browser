# Set up Ghost Browser

This is the canonical setup document for coding agents. Day-to-day behavior comes from `ghost-browser skill`.

## 1. Check prerequisites

Require Python 3.11 or newer and `uv`. Confirm only whether `APIFY_TOKEN` exists; never print its value,
persist it in `.env`, or place it in a command argument.

## 2. Install

From a local checkout:

```sh
uv tool install --python 3.12 --editable .
```

From the published repository:

```sh
uv tool install --python 3.12 --upgrade git+https://github.com/yfe404/ghost-browser.git
```

## 3. Register the optional skill

If the current coding agent supports skills, create its normal user skill directory named `ghost-browser`
and write the output of this command to `SKILL.md`:

```sh
ghost-browser skill
```

Do not edit bundled or vendor plugin caches.

## 4. Verify without leaking connection details

Allocate one browser, ask Chrome for its product string, and release it even if verification fails:

```sh
ghost-browser <<'PY'
print(cdp("Browser.getVersion")["product"])
PY
ghost-browser stop
```

Successful output contains a Chromium product string. Do not inspect daemon files or print Gateway/CDP URLs.
Setup is complete after `ghost-browser status` reports `stopped`.

For normal tasks, keep one browser alive across invocations and run `ghost-browser stop` exactly once at the
end. Do not probe `/json/version`, `/json`, or `/json/list` yourself because each request can allocate and bill
another browser.
