# Crypto Trading Bot

## Installatie

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Tests

```bash
source .venv/bin/activate
pytest -v tests/
```

## Starten

### Demo / Dry-Run (synthetische OHLCV zonder API-keys)
```bash
source .venv/bin/activate
python -m src.runner --demo
```

### Enkele cyclus (live openbare data / paper mode)
```bash
source .venv/bin/activate
python -m src.runner --once
```

### Continue loop
```bash
source .venv/bin/activate
python -m src.runner --interval 60
```
