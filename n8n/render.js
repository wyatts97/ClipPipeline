// Fill workflow.template.json and the credentials from the environment.
// Usage: node render.js <output-dir>   (writes credentials.json and workflow.json there)
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const out = process.argv[2];
if (!out) throw new Error('usage: node render.js <output-dir>');
const env = process.env;

const TELEGRAM_CREDENTIAL_ID = 'clipbotTelegram1';
const TOKEN_CREDENTIAL_ID = 'clipbotHeaderTk1';

const credentials = [
  {
    id: TELEGRAM_CREDENTIAL_ID,
    name: 'ClipBot Telegram',
    type: 'telegramApi',
    data: {
      accessToken: env.TELEGRAM_BOT_TOKEN,
      baseUrl: (env.TELEGRAM_API_URL || 'https://api.telegram.org').replace(/\/$/, ''),
    },
  },
  {
    id: TOKEN_CREDENTIAL_ID,
    name: 'ClipBot token',
    type: 'httpHeaderAuth',
    data: { name: 'X-Clipbot-Token', value: env.CLIPBOT_TOKEN },
  },
];

const replacements = {
  __TELEGRAM_CREDENTIAL_ID__: TELEGRAM_CREDENTIAL_ID,
  __TOKEN_CREDENTIAL_ID__: TOKEN_CREDENTIAL_ID,
  __CLIPBOT_URL__: (env.CLIPBOT_URL || 'http://clipbot:8080').replace(/\/$/, ''),
  // Random per install, so the public Telegram webhook path can't be guessed.
  __TELEGRAM_WEBHOOK_ID__: crypto.randomUUID(),
  __EVENTS_WEBHOOK_ID__: crypto.randomUUID(),
};

let text = fs.readFileSync(path.join(__dirname, 'workflow.template.json'), 'utf8');
for (const [placeholder, value] of Object.entries(replacements)) {
  text = text.split(placeholder).join(value);
}
const workflow = JSON.parse(text);
const leftover = text.match(/__[A-Z_]+__/);
if (leftover) throw new Error(`unfilled placeholder ${leftover[0]}`);

fs.writeFileSync(path.join(out, 'credentials.json'), JSON.stringify(credentials, null, 2), { mode: 0o600 });
fs.writeFileSync(path.join(out, 'workflow.json'), JSON.stringify([workflow], null, 2));
console.log(`rendered workflow "${workflow.name}" (${workflow.nodes.length} nodes)`);
