# MegaFon for Home Assistant

Custom integration: MegaFon (Russian mobile operator) balance, cashback, package
remainders and tariff info in Home Assistant.

Sign in with your phone number + SMS code — same as the mobile personal account.

## Sensors

| Sensor | Description |
|---|---|
| Balance | Current balance, ₽ |
| Cashback | Accumulated cashback, ₽ |
| Minutes left | Remaining voice minutes (attributes: total, % left, unlimited) |
| Internet left | Remaining data, GB |
| SMS left | Remaining SMS package |
| Tariff | Tariff name (attributes: start date, tariff id) |
| Tariff price | Monthly fee, ₽ (attributes: per-component breakdown) |
| Next charge date | Date and time of the next charge |
| Next charge amount | Amount to be charged, ₽ |

## Installation

### Via HACS (recommended)

1. **HACS** → ⋮ menu → **Custom repositories** → add
   `https://github.com/tx1983/ha-megafon-lk`, category **Integration**.
2. Find "**MegaFon**" in the list → **Download**.
3. Restart Home Assistant.

### Manually

1. Download `megafon_lk.zip` from the [latest release](https://github.com/tx1983/ha-megafon-lk/releases).
2. Extract it so the `megafon_lk` folder ends up in `<config>/custom_components/`.
3. Restart Home Assistant.

## Setup

**Settings → Devices & Services → Add Integration → "MegaFon"**

### Step 1. Phone number + x-cabinet parameters

Besides the phone number, the integration asks for three UUIDs —
`x-cabinet-check-info`, `x-cabinet-id-param` and
`x-cabinet-validation-param`. These are device parameters the MegaFon API
**validates**: without real values it authorizes fine but answers data requests
with an empty `{}`. You only need to enter them **once** — afterwards the
session is kept alive by SMS re-auth alone.

How to get them (2 minutes):

1. Open [lk.megafon.ru](https://lk.megafon.ru) and log in.
2. **F12** → **Network** tab → reload the page.
3. Pick any request to `api.megafon.ru` (e.g. `remainders/mini`).
4. In **Headers → Request Headers** copy the values of
   `x-cabinet-check-info` / `x-cabinet-id-param` / `x-cabinet-validation-param`.
5. Paste them into the field — either the three DevTools lines or the whole
   request (**Copy → Copy as cURL**) — the UUIDs are parsed automatically.

### Step 2. SMS code

A 6-digit code arrives by SMS — enter it. Done.

### Re-authentication

The session lasts a long time, but MegaFon occasionally kills it. The
integration then shows a re-authentication prompt: press **Submit** and an SMS
with a new code is sent (the code is only requested when you confirm — no
surprise messages). The x-cabinet parameters are kept, no need to re-enter.

### Fallback login (captcha)

If MegaFon answers the SMS request with a captcha, a fallback step opens:
sign in using browser cookies (Cookie + the same three headers).

## How it works

- Authentication uses the same JSON endpoints as the React personal account:
  `sessionCheck → otp/request → otp/submit`.
- `X-Cabinet-Access-Token` lives ~20 minutes; the integration refreshes it via
  `sessionCheck` (the token arrives in the `X-AUTH-TOKEN` response header and
  inside the body — same as the browser SPA).
- Rotated cookies are persisted in the config entry — restarting Home
  Assistant does not kill the session.
- If the API answers an `a1*` error, the integration goes to re-auth instead
  of silently showing `unknown`.

## Debugging

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.megafon_lk: debug
```

The debug log shows API responses (`->`), the cookies being sent and the
response headers.

## Limitations

- Multi-SIM / child numbers are untested — the master account context is used.
- `x-cabinet-*` parameters are bound to your device/account; if MegaFon ever
  revokes them you will need to paste fresh ones.

## Disclaimer

This project is not affiliated with or endorsed by MegaFon. Use at your own risk.

## Support the project

If this integration saves you time, you can
[buy me a coffee](https://pay.cloudtips.ru/p/a039d481).

## License

MIT — see [LICENSE](LICENSE).
