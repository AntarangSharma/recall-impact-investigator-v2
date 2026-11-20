from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
GUIDE = "https://www.cpsc.gov/Recalls/CPSC-Recalls-Application-Program-Interface-API-Information"
API = "https://www.saferproducts.gov/RestWebServices/Recall"
ORIGINS = {"cpsc.gov", "www.cpsc.gov", "saferproducts.gov", "www.saferproducts.gov"}
MAX_RESPONSE = 512_000
MAX_SOURCE_CHARS = 8_000
SCOPE_VERSION = 2


@dataclass(frozen=True)
class Notice:
    id: str
    brand: str
    slug: str

    @property
    def url(self) -> str:
        year = 2000 + int(self.id[:2])
        return f"https://www.cpsc.gov/Recalls/{year}/{self.slug}"

    @property
    def api_url(self) -> str:
        return f"{API}?{urlencode({'RecallNumber': self.id.replace('-', ''), 'format': 'json'})}"


NOTICES = {
    notice.id: notice
    for notice in (
        Notice(
            "23-794",
            "Secura",
            "Secura-Recalls-Air-Fryers-Due-to-Fire-and-Burn-Hazards-Recall-Alert",
        ),
        Notice(
            "23-130",
            "Cosori",
            "Two-Million-COSORI-Air-Fryers-Recalled-by-Atekcity-Due-to-Fire-and-Burn-Hazards",
        ),
        Notice(
            "24-159",
            "Insignia",
            "Best-Buy-Recalls-Insignia-Air-Fryers-and-Air-Fryer-Ovens-Due-to-Fire-Burn-and-Laceration-Hazards",
        ),
    )
}


class SourceError(ValueError):
    pass


class Snapshot(BaseModel):
    notice_id: str
    url: str
    api_url: str
    retrieved_at: datetime
    source_hash: str
    html_hash: str
    api_hash: str
    text: str
    scope_version: int = 1

    @property
    def stale(self) -> bool:
        return (datetime.now(UTC) - self.retrieved_at).days >= 7


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(text: str) -> str:
    return " ".join(text.split())


def validate_url(url: str, kind: str) -> None:
    parsed = urlsplit(url)
    allowed_path = (
        parsed.path.lower() == "/restwebservices/recall"
        if kind == "api"
        else re.fullmatch(r"/Recalls/20\d{2}/[a-zA-Z0-9-]+/?", parsed.path) is not None
    )
    allowed_hosts = (
        {"www.saferproducts.gov", "saferproducts.gov"}
        if kind == "api"
        else {"www.cpsc.gov", "cpsc.gov"}
    )
    if (
        parsed.scheme != "https"
        or parsed.netloc not in ORIGINS
        or parsed.netloc not in allowed_hosts
        or not allowed_path
        or parsed.fragment
    ):
        raise SourceError("Source URL or redirect was outside the approved CPSC origins and paths.")


async def fetch(client: httpx.AsyncClient, url: str, kind: str) -> tuple[bytes, str]:
    for _ in range(4):
        validate_url(url, kind)
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers["location"])
                continue
            response.raise_for_status()
            media = response.headers.get("content-type", "").partition(";")[0].strip().lower()
            if media != ("application/json" if kind == "api" else "text/html"):
                raise SourceError("CPSC returned an unexpected content type.")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE:
                    raise SourceError("CPSC response exceeded the size limit.")
            return bytes(body), url
    raise SourceError("CPSC exceeded the redirect limit.")


def section(fields: dict[str, str], name: str) -> str:
    value = fields.get(name, "")
    if not value:
        raise SourceError(f"The notice is missing its {name} section.")
    return value


def extract_html(html: bytes) -> tuple[str, str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.select("script, style, template, noscript"):
        element.decompose()
    title = soup.select_one("h1")
    sections = soup.select(".recall-product__details")
    if title is None or len(sections) != 1:
        raise SourceError("The CPSC title or recall details could not be identified.")
    details = sections[0]
    fields: dict[str, str] = {}
    for box in details.select(".view-rows"):
        heading = box.select_one(":scope > .recall-product__field-title")
        if heading is None:
            continue
        label = normalize(heading.get_text()).rstrip(":")
        if label in fields:
            raise SourceError("CPSC returned duplicate notice sections.")
        index = box.index(heading)
        heading.extract()
        fields[label] = normalize(box.get_text(" "))
        if label == "Description":
            # Keep the matching description on its own line without duplicating evidence quotes.
            box.decompose()
        else:
            box.insert(index, heading)
    description = section(fields, "Description")
    for label in ("Remedy", "Sold At", "Recall number"):
        section(fields, label)
    selected = [f"Description: {description}", normalize(details.get_text(" "))]
    note = soup.select_one("#block-recallcommissionerstatement")
    if note:
        selected.append(normalize(note.get_text(" ")))
    text = normalize(title.get_text(" ")) + "\n" + "\n".join(selected)
    if len(text) > MAX_SOURCE_CHARS:
        raise SourceError("CPSC notice text exceeded the supported size.")
    return text, description, fields["Recall number"]


def read_response(
    notice: Notice, html: bytes, api_body: bytes, url: str, retrieved_at: datetime
) -> Snapshot:
    validate_url(url, "html")
    text, description, notice_id = extract_html(html)
    try:
        records = json.loads(api_body)
        rows = [row for row in records if row["RecallNumber"] == notice.id.replace("-", "")]
        if len(rows) != 1 or not isinstance(rows[0]["Description"], str):
            raise SourceError("CPSC API discovery did not identify one matching notice.")
        api_record = rows[0]
        api_page = api_record["URL"].rstrip("/")
        # The Insignia API record contains one literal space in the canonical slug.
        if notice.id == "24-159" and api_page == notice.url.replace(
            "Insignia-Air-Fryers", "Insignia-Air Fryers"
        ):
            api_page = notice.url
        validate_url(api_page, "html")
        if url.rstrip("/") != notice.url or api_page != notice.url:
            raise SourceError("CPSC sources disagree about the canonical notice URL.")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("CPSC API discovery returned an unexpected response.") from exc
    if notice_id != notice.id:
        raise SourceError("CPSC returned a different recall number.")
    if normalize(api_record["Description"]) != description:
        raise SourceError(
            "CPSC API and HTML descriptions disagree. Record this source as incomplete."
        )
    return Snapshot(
        notice_id=notice.id,
        url=url,
        api_url=notice.api_url,
        retrieved_at=retrieved_at,
        source_hash=sha(text.encode()),
        html_hash=sha(html),
        api_hash=sha(api_body),
        text=text,
        scope_version=SCOPE_VERSION,
    )


async def retrieve(notice: Notice, transport: httpx.AsyncBaseTransport | None = None) -> Snapshot:
    try:
        async with (
            asyncio.timeout(12),
            httpx.AsyncClient(
                transport=transport,
                timeout=5,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "RecallImpactInvestigator/0.1 (public recall notices)"},
            ) as client,
        ):
            (api_body, _), (html, url) = await asyncio.gather(
                fetch(client, notice.api_url, "api"),
                fetch(client, notice.url, "html"),
            )
        return read_response(notice, html, api_body, url, datetime.now(UTC))
    except (httpx.HTTPError, TimeoutError, KeyError) as exc:
        raise SourceError("CPSC retrieval failed or timed out.") from exc


def load_fixture(notice_id: str) -> Snapshot:
    snapshot = Snapshot.model_validate_json((DATA / "notices" / f"{notice_id}.json").read_text())
    validate_url(snapshot.url, "html")
    if snapshot.notice_id != notice_id or snapshot.source_hash != sha(snapshot.text.encode()):
        raise SourceError("Stored fixture failed its identity or integrity check.")
    return snapshot
