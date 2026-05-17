# Avatar ALL-TEXT 45sec Generator

Standalone form with live duration counter for the Avatar ALL-TEXT 45sec Generator n8n workflow.

## Form

**https://owocarz.github.io/avatar-alltext-45sec/form.html**

- Paste any football match narration (any league — Premier League, La Liga, Champions League, etc.)
- Live counter estimates video duration as you type (~2.6 words/sec, range 20-60 sec)
- Select language (English / French / Portuguese / Swahili)
- Select whether to show text overlay
- Submit sends directly to the n8n workflow

## How it works

1. Form POSTs to n8n formTrigger webhook
2. n8n detects teams from narration text automatically
3. Fetches YouTube highlights background via yt-bridge (residential IP on user PC)
4. HeyGen renders avatar speaking the narration
5. Shotstack composites avatar + background + captions
6. Final video delivered via WhatsApp

## Duration formula



HeyGen speed=1.15 -> ~2.6 words/sec. Clamped to [20, 60] seconds.
