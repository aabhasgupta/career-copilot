# Email integration setup

Career Copilot uses your own mailbox for everything email-related: sending yourself the daily
digest and urgent alerts, and (Phase 4) reading replies from companies to keep application
states current. There is no third-party email service in the middle - the app talks directly
to your provider's API as *you*, with a token stored only on your machine.

Two providers are planned behind one interface (`src/copilot/email_agent/provider.py`),
selected in `profile.yaml`:

| Provider | Status | For |
|---|---|---|
| `outlook` | **Built and verified live** | Hotmail, Outlook.com, Live.com (personal Microsoft accounts) |
| `gmail` | Planned, not yet built | Gmail / Google accounts (see the last section) |

## Outlook / Hotmail via Microsoft Graph

### How auth works (so the steps below make sense)

The app uses the **device-code flow**: `copilot email login` prints a URL and a short code,
you enter the code in any browser while signed in to your Microsoft account, and Microsoft
hands the app a token. There is **no password stored, no client secret anywhere** - the flow
is designed for apps that can't keep secrets. The token (with its refresh token) is cached at
`data/email_token.json` (gitignored) and silently renewed from then on; the interactive login
is a one-time step.

The one piece of setup Microsoft requires is a free **app registration** - essentially telling
Azure "an app by this name will be asking users for these permissions." It costs nothing and
needs no Azure subscription.

### Step 1 - create the app registration (one time, ~5 minutes)

1. Go to [portal.azure.com](https://portal.azure.com) and sign in with any Microsoft account
   (it does not need to be the mailbox account).
2. Search for **App registrations** -> **New registration**.
3. Name: anything (e.g. `career-copilot`).
4. **Supported account types**: choose **"Personal Microsoft accounts only"**. This matters -
   the code authenticates against the `consumers` endpoint, which only personal-account
   registrations accept.
5. Redirect URI: leave **empty** (device-code flow doesn't use one).
6. Click **Register**, then copy the **Application (client) ID** from the overview page -
   you'll need it in step 4.

### Step 2 - enable public client flows

1. In your new registration, open **Authentication** (left sidebar).
2. Under **Advanced settings**, set **"Allow public client flows"** to **Yes** and save.
   Device-code login fails with a cryptic error without this.

### Step 3 - grant API permissions

1. Open **API permissions** -> **Add a permission** -> **Microsoft Graph** ->
   **Delegated permissions**.
2. Add all four:
   - `Mail.Send` - digest and alert emails
   - `Mail.Read` - inbox sync reads recent messages
   - `Mail.ReadWrite` - filing processed emails into the "Career Copilot" folder
   - `MailboxSettings.ReadWrite` - creating the per-type Outlook categories
     (Rejection, Interview Invite, ...)
3. No admin consent is needed for a personal account - you consent yourself during login.

Grant all four up front even if you only want the digest today: consenting once during login
covers everything, and skipping some means redoing the login when inbox sync needs them.

### Step 4 - wire it into Career Copilot

```sh
# .env (repo root, gitignored)
AZURE_CLIENT_ID=<the Application (client) ID from step 1>
```

```yaml
# profile.yaml
email_integration:
  provider: outlook
  address: you@hotmail.com   # where digests and alerts get sent
```

### Step 5 - log in and verify

```sh
uv run copilot email login   # prints a URL + code; enter it in a browser, once
uv run copilot email test    # sends yourself a test email - confirms auth and sending
uv run copilot digest --dry-run   # digest content without sending
```

If `email test` lands in your inbox, everything works. The token in `data/email_token.json`
renews itself; you should not see the device-code prompt again unless you revoke access or
delete that file.

### Step 6 (optional) - turn on inbox monitoring

```sh
uv run copilot inbox sync --dry-run  # see what sync would do - writes nothing
uv run copilot inbox sync            # real run
uv run copilot schedule install      # hourly background sync + digest via launchd
```

See the README's "Inbox monitoring" section for what sync does and how the review queue
works. First run looks back 7 days only - old mail is never touched automatically, but you
can drag any email (however old) into the **"Career Copilot/Process"** folder that sync
creates, and the next run will process it. Pause anytime with `copilot schedule pause` or
the dashboard's Inbox sync card.

### Troubleshooting

- **`AADSTS7000218` / public client error at login** - step 2 was skipped: enable
  "Allow public client flows".
- **`unauthorized_client` or account-type error** - the registration wasn't created as
  "Personal Microsoft accounts only" (step 1.4). Easiest fix: create a fresh registration
  with the right account type; there's nothing to migrate.
- **`Cached login expired or invalid`** - run `copilot email login` again. This can happen
  if the token cache file was deleted or Microsoft revoked the grant.
- **Login works but sends fail with 403** - a permission from step 3 is missing; add it and
  log in again to re-consent.

## Gmail (planned - not built yet)

The `EmailProvider` interface was designed so a Gmail implementation slots in without touching
the rest of the app, but the code does not exist yet - there's been no real Gmail inbox to
verify it against, and this project's convention is to never ship integrations verified only
on paper. If/when it's built, `profile.yaml` would switch with:

```yaml
email_integration:
  provider: gmail
  address: you@gmail.com
```

What the setup will look like (for expectation-setting, not instructions):

1. **Google Cloud console** instead of Azure: create a (free) project, enable the **Gmail
   API**, configure an OAuth consent screen (external, test mode is fine for one user), and
   create an **OAuth client ID** of type "Desktop app". This is the moral equivalent of the
   Azure app registration.
2. **Scopes**: `gmail.send` (digest/alerts), `gmail.readonly` or `gmail.modify` (inbox sync +
   filing). Note Gmail's restricted-scope review applies to published apps; a personal
   test-mode app avoids it.
3. **Auth flow**: Google's equivalent one-time browser consent, token cached locally - same
   user experience as `copilot email login`.
4. **Concept mapping** (why the provider interface speaks Copilot vocabulary, not Outlook's):
   Outlook's *folder + categories* become Gmail *labels* - "file into Career Copilot with
   category Rejection" maps to applying two labels. Nothing outside `email_agent/gmail.py`
   would care about the difference.

If you need Gmail support, say so - the interface is ready for it, and the work is scoped in
docs/PLAN.md's stack notes.
