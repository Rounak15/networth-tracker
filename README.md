# Net worth tracker

A private net worth tracker that runs entirely on your own computer. Track stocks, ETFs, mutual funds,
gold bonds, fixed deposits and savings in one place, import your broker and bank spreadsheets, and see
your allocation and month-by-month history. Nothing is sent anywhere: your data is stored locally,
encrypted with a password you choose.

## Features

- Dashboard with net worth, allocation (equity, debt, gold, cash), monthly history and FD maturities
- Add, edit and delete holdings by hand, with automatic FD interest calculation
- Import spreadsheets with a preview first, duplicate rejection and opt-in removal of sold holdings
  - Zerodha Console holdings (.xlsx)
  - ICICI Bank FD summary (.xlsx)
  - HDFC Bank FD summary (.xlsx)
  - SBI Mutual Fund account summary (.ods or .xlsx)
  - Any sheet or CSV with columns such as Name, ISIN, Quantity, Average Price, Price
- Light and dark themes, sortable holdings, and a privacy toggle that hides all amounts
- Data is encrypted at rest (AES-256-GCM, key derived from your password with scrypt)

## Getting started

You need Python 3.10 or newer.

```
git clone https://github.com/Rounak15/networth-tracker.git
cd networth-tracker
```

**Windows:** double-click `run.bat`

**Linux / macOS:**

```
chmod +x run.sh
./run.sh
```

The launcher creates a virtual environment, installs the dependencies and starts the app. Your browser
opens at http://networth.localhost:8000 once it is ready. On first launch you create a password. There is
no recovery if you forget it, so keep it safe.

Use **Log out** in the sidebar to stop the app.

## Your data stays yours

Everything you enter or import is saved in a `data/` folder next to `app.py`. That folder is created on
first run, is encrypted, and is excluded from Git by `.gitignore`. Spreadsheets (`.xlsx`, `.xls`, `.ods`,
`.csv`) are excluded too, so personal files in the project folder are never committed by accident.
