import csv
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from recall.sources import DATA, ROOT


@contextmanager
def server(port, directory):
    env = dict(os.environ, RECALL_DATA_DIR=str(directory))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "recall.app:app_factory",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError(f"Demo server exited with status {process.returncode}.")
            try:
                if httpx.get(f"{base}/health", timeout=0.5).is_success:
                    break
            except httpx.RequestError:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("Demo server did not become healthy.")
        yield base
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.browser
def test_secura_upload_review_restart_and_resume(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    screenshot_dir = os.getenv("RECALL_SCREENSHOTS")
    target = Path(screenshot_dir) if screenshot_dir else None
    if target:
        target.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        with server(port, tmp_path) as base:
            page.goto(base)
            page.get_by_label("Inventory CSV").set_input_files(DATA / "demo.csv")
            page.get_by_label("Notice scope").select_option("23-794")
            page.get_by_role("button", name="Investigate inventory").click()
            expect(page.get_by_role("heading", name="Inventory review")).to_be_visible()
            expect(page.locator(".total.affected_by_notice strong")).to_have_text("1")
            expect(page.locator(".total.excluded_by_notice strong")).to_have_text("1")
            expect(page.locator(".total.needs_information strong")).to_have_text("1")
            expect(page.get_by_text("What is the date code for DEMO-003?")).to_be_visible()
            page.get_by_label("Evidence for DEMO-001").click()
            expect(page.locator("blockquote").first).to_have_text(
                "SAF-53D (TXG-DT16E) with date code 1901"
            )
            if target:
                page.screenshot(path=target / "review.png", full_page=True, animations="disabled")
            review_path = page.url
        with server(port, tmp_path):
            page.goto(review_path)
            expect(page.get_by_role("button", name="Save and resume")).to_be_visible()
            page.get_by_label("Date code", exact=True).fill("1901")
            page.get_by_role("button", name="Save and resume").click()
            expect(page.get_by_role("status")).to_have_text(
                "Identifier saved. The affected findings were reevaluated.",
            )
            expect(page.locator(".total.affected_by_notice strong")).to_have_text("2")
            expect(page.locator(".total.needs_information strong")).to_have_text("0")
            page.get_by_label("Evidence for DEMO-003").click()
            expect(page.locator(".unit[open] blockquote")).to_have_text(
                "SAF-53D (TXG-DT16E) with date code 1901"
            )
            if target:
                page.screenshot(path=target / "resumed.png", full_page=True, animations="disabled")
            with page.expect_download() as download:
                page.get_by_role("link", name="Export CSV").click()
            with download.value.path().open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            assert len(rows) == 3
            assert any(
                row["stock_id"] == "DEMO-003" and row["finding"] == "affected_by_notice"
                for row in rows
            )
            page.set_viewport_size({"width": 390, "height": 840})
            assert page.evaluate("document.documentElement.scrollWidth === window.innerWidth")
            assert errors == []
        browser.close()
