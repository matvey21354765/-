#!/bin/bash
# Install Chrome for Playwright if not present
playwright install chrome --with-deps 2>/dev/null || true
python main.py
