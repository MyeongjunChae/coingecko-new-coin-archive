"""Live verification for the portfolio/favorites fix against the real Chrome.

Usage (from the project root):
    python collector\\verify_live.py                 # inspection only
    python collector\\verify_live.py --apply         # also click the + row
    python collector\\verify_live.py --apply --coin zaza-3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parent
if str(COLLECTOR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

import coingecko_archive as archive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coin", default="bitcoin")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    endpoint = archive.chrome_debug_endpoint("chrome")
    print(f"endpoint: {endpoint}")
    if not endpoint:
        print("DevToolsActivePort 를 읽지 못했습니다.")
        return 1
    relay = archive.ensure_relay(endpoint)
    if relay:
        endpoint = relay
        print(f"relay: {endpoint}")

    coin = archive.Coin(
        name=args.coin,
        url=f"https://www.coingecko.com/en/coins/{args.coin}",
        portfolio="inactive",
        page=1,
    )

    with sync_playwright() as playwright:
        try:
            browser = archive._connect_live_browser(playwright, endpoint)
        except Exception as error:  # noqa: BLE001
            print(f"연결 실패: {type(error).__name__}: {error}")
            return 2
        print("연결 성공")

        contexts = browser.contexts or [browser.new_context()]
        context = contexts[0]
        for candidate in contexts:
            if any("coingecko.com" in (page.url or "") for page in candidate.pages):
                context = candidate
                break

        page = context.new_page()
        try:
            page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        try:
            archive._navigate(page, coin.url)
            print(f"인증 상태: {archive._is_authenticated(page)}")

            snapshot = archive._portfolio_snapshot(page)
            portfolios = snapshot.get("portfolios") if snapshot.get("ok") else None
            print(f"포트폴리오 스냅샷: ok={snapshot.get('ok')} 개수={len(portfolios or [])}")
            if portfolios:
                for item in portfolios[-5:]:
                    print(f"  {item.get('title')!r} id={item.get('id')} coins={item.get('count')}")
                newest = max(
                    portfolios,
                    key=lambda item: archive._portfolio_label_number(str(item.get("title", ""))) or -1,
                )
                print(f"최신 폴더: {newest.get('title')!r} ({newest.get('count')}개)")

            state = archive._open_coin_portfolio_picker(page, coin)
            print(f"드롭다운 상태: {state}")

            info = archive._portfolio_dropdown_info(page)
            print(f"드롭다운 ok={info.get('ok')} visible={info.get('dropdownVisible')} "
                  f"button={info.get('buttonText')!r}")
            for row in info.get("rows", [])[-6:]:
                print(
                    f"  row {row.get('portfolioId')} {row.get('title')!r} "
                    f"plus={row.get('plus')} check={row.get('checked')}"
                )

            if args.apply and state == "opened" and portfolios:
                import time

                started = time.time()
                results = archive.add_coins_to_favorites(page, [coin])
                print(f"즐겨찾기 결과 ({time.time() - started:.1f}s):")
                print(json.dumps(results, ensure_ascii=False, indent=2))
                return 0 if any(r.get("status") in {"added", "already_active"} for r in results) else 3
            return 0
        finally:
            page.close()
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
