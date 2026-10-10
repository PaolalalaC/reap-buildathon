# Crypto Treasury Agent — Reap × AIBM Buildathon

B2B treasury agent: invoice guardrails (Reap merchant allowlist + **$700** cap), on-chain USDC (Ethereum Sepolia) plus ETH Sepolia collateral, then Reap Agentic quote/checkout.

## Demo

```bash
cp .env.example .env   # add REAP_API_KEY and TREASURY_WALLET_ADDRESS
python3 agent_logic.py --serve
```

Open http://127.0.0.1:8765/

| Case | Merchant | Amount | Result |
|---|---|---|---|
| 1 | beautybakerie.com | ~$11 / quote ~$20 | 20 USDC covers → Reap hosted approval |
| 2 | abchome.com | $279 / quote ~$303 | Lock ~0.11 ETH collateral → Reap approval |
| 3 | headphones.com | $699 | Under cap, ETH short → no Reap |
| 4 | gmktec.com | $6,599 | Over $700 policy cap → no Reap |

Approval is Reap `collect.prava.space`, not the merchant Shopify cart.

## CLI

```bash
python3 agent_logic.py --catalog beautybakerie --skip-enroll --live
python3 agent_logic.py --catalog abchome --skip-enroll --live
python3 agent_logic.py --catalog headphones.com --skip-enroll
python3 agent_logic.py --catalog gmktec --skip-enroll
```

Do not commit `.env`. Sandbox card PAN/CVV stay on Reap’s hosted page only.
