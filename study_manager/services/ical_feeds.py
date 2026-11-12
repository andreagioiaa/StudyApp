"""Calendari in sola lettura tramite indirizzo iCal (nessuna API, nessun login).

Ogni "feed" e' l'URL di un file ``.ics`` sempre aggiornato, per esempio
l'"Indirizzo segreto in formato iCal" di un calendario Google. StudyFlow lo
scarica a ogni sincronizzazione, lo legge con lo stesso parser dell'import
``.ics`` e applica upsert + cancellazione speculare sugli eventi del feed.

- I metadati (nome, materia, colore, stato) stanno in ``settings.ical_feeds``.
- Gli URL sono segreti (chi li conosce vede il calendario): vengono cifrati con
  DPAPI in ``settings.ical_feed_urls_enc``, esclusi dal backup e mai scritti
  nei log.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from ..database import EVENT_SOURCE_ICAL, Database, normalize_hex
from ..logging_setup import mask_secrets
from ..utils import LOCAL_DATETIME_FMT
from .google_auth import GoogleOfflineError, _is_network_error, protect_secret, unprotect_secret
from .ics_import import calendar_name, parse_ics_bytes


LOGGER = logging.getLogger(__name__)

SETTING_FEEDS = "ical_feeds"
SETTING_FEED_URLS = "ical_feed_urls_enc"
CALENDAR_PREFIX = "ical:"
MAX_FEED_BYTES = 20 * 1024 * 1024
HTTP_TIMEOUT = (8, 40)
# Il parser .ics espande le ricorrenze da 31 giorni fa in avanti: la
# cancellazione speculare lavora nella stessa finestra.
RECURRENCE_PAST_DAYS = 31

# Colori dei calendari Google, assegnati a turno ai nuovi feed.
FEED_COLORS: tuple[str, ...] = (
    "#4285F4",
    "#0B8043",
    "#D50000",
    "#F4511E",
    "#8E24AA",
    "#039BE5",
    "#33B679",
    "#E67C73",
    "#F6BF26",
    "#3F51B5",
    "#7986CB",
    "#616161",
)


class ICalFeedError(ValueError):
    """Errore mostrabile all'utente (URL non valido, feed non raggiungibile...)."""


def normalize_feed_url(url: str) -> str:
    text = str(url or "").strip()
    if text.lower().startswith("webcal://"):
        text = "https://" + text[9:]
    parts = urlsplit(text)
    if parts.scheme not in {"https", "http"} or not parts.netloc:
        raise ICalFeedError(
            "Incolla l'indirizzo completo, che inizia con https:// (o webcal://)."
        )
    if len(text) > 2000:
        raise ICalFeedError("L'indirizzo e' troppo lungo.")
    return text


def mask_feed_url(url: str) -> str:
    """Versione mostrabile dell'URL: dominio e inizio del percorso, senza segreti."""
    parts = urlsplit(str(url or ""))
    segments = [segment for segment in parts.path.split("/") if segment]
    shown: list[str] = []
    for segment in segments:
        lowered = segment.lower()
        if lowered.startswith("private-") or len(segment) > 24:
            shown.append(segment[:8] + "…")
        else:
            shown.append(segment)
    text = parts.netloc + ("/" + "/".join(shown) if shown else "")
    return mask_secrets(text)[:90]


def calendar_id_for(feed_id: str) -> str:
    return f"{CALENDAR_PREFIX}{feed_id}"


def default_fetch(url: str) -> bytes:
    """Scarica il contenuto ``.ics`` (limite 20 MB)."""
    import requests

    try:
        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            stream=True,
            headers={"User-Agent": "StudyFlow-Desktop", "Accept": "text/calendar, */*"},
        )
    except Exception as exc:
        if _is_network_error(exc):
            raise GoogleOfflineError("Calendario iCal non raggiungibile.") from exc
        raise ICalFeedError(f"Download non riuscito: {mask_secrets(exc)}") from exc
    with response:
        if response.status_code in (401, 403, 404, 410):
            raise ICalFeedError(
                "Indirizzo non valido o reimpostato (errore "
                f"{response.status_code}). Copia di nuovo l'indirizzo segreto iCal."
            )
        if response.status_code >= 400:
            raise ICalFeedError(f"Il server ha risposto con l'errore {response.status_code}.")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(65536):
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                raise ICalFeedError("Il calendario supera il limite di 20 MB.")
            chunks.append(chunk)
    data = b"".join(chunks)
    if b"BEGIN:VCALENDAR" not in data[:4096].upper():
        raise ICalFeedError("L'indirizzo non restituisce un calendario iCal (.ics).")
    return data


class ICalFeedStore:
    """Elenco dei feed iCal salvato nelle impostazioni."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- lettura
    def list_feeds(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.db.get_setting(SETTING_FEEDS) or "[]")
        except ValueError:
            raw = []
        urls = self._urls()
        feeds: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            feed = {
                "id": str(item["id"]),
                "name": str(item.get("name") or ""),
                "subject": str(item.get("subject") or ""),
                "color": normalize_hex(item.get("color")) or FEED_COLORS[0],
                "url_hint": str(item.get("url_hint") or ""),
                "error": str(item.get("error") or ""),
                "last_ok": str(item.get("last_ok") or ""),
            }
            feed["has_url"] = feed["id"] in urls
            feeds.append(feed)
        return feeds

    def has_feeds(self) -> bool:
        return any(feed["has_url"] for feed in self.list_feeds())

    def get(self, feed_id: str) -> dict[str, Any] | None:
        for feed in self.list_feeds():
            if feed["id"] == feed_id:
                return feed
        return None

    def for_calendar_id(self, calendar_id: str | None) -> dict[str, Any] | None:
        text = str(calendar_id or "")
        if not text.startswith(CALENDAR_PREFIX):
            return None
        return self.get(text[len(CALENDAR_PREFIX):])

    def for_subject(self, subject: str) -> dict[str, Any] | None:
        target = str(subject or "").strip().casefold()
        if not target:
            return None
        for feed in self.list_feeds():
            if feed["subject"].strip().casefold() == target:
                return feed
        return None

    def url(self, feed_id: str) -> str:
        stored = self._urls().get(feed_id, "")
        return unprotect_secret(stored) if stored else ""

    def _urls(self) -> dict[str, str]:
        try:
            value = json.loads(self.db.get_setting(SETTING_FEED_URLS) or "{}")
        except ValueError:
            return {}
        return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}

    # ------------------------------------------------------------ scrittura
    def _save(self, feeds: list[dict[str, Any]], urls: dict[str, str] | None = None) -> None:
        keep = ("id", "name", "subject", "color", "url_hint", "error", "last_ok")
        values = {
            SETTING_FEEDS: json.dumps(
                [{key: feed.get(key, "") for key in keep} for feed in feeds], ensure_ascii=False
            )
        }
        if urls is not None:
            values[SETTING_FEED_URLS] = json.dumps(urls)
        self.db.set_settings(values)

    def add_feed(self, url: str, subject: str = "", color: str | None = None, name: str = "") -> str:
        clean_url = normalize_feed_url(url)
        feeds = self.list_feeds()
        urls = self._urls()
        for feed in feeds:
            if feed["has_url"] and self.url(feed["id"]) == clean_url:
                raise ICalFeedError("Questo indirizzo iCal e' gia' presente.")
        feed_id = secrets.token_hex(6)
        used = {feed["color"] for feed in feeds}
        chosen = normalize_hex(color) or next(
            (candidate for candidate in FEED_COLORS if candidate not in used),
            FEED_COLORS[len(feeds) % len(FEED_COLORS)],
        )
        feeds.append(
            {
                "id": feed_id,
                "name": str(name or "").strip()[:150],
                "subject": str(subject or "").strip()[:150],
                "color": chosen,
                "url_hint": mask_feed_url(clean_url),
                "error": "",
                "last_ok": "",
            }
        )
        urls[feed_id] = protect_secret(clean_url)
        self._save(feeds, urls)
        if subject:
            self.db.set_subject_color(subject, chosen)
        return feed_id

    def update_feed(self, feed_id: str, **changes: Any) -> None:
        feeds = self.list_feeds()
        for feed in feeds:
            if feed["id"] != feed_id:
                continue
            for key in ("name", "subject", "error", "last_ok"):
                if key in changes:
                    feed[key] = str(changes[key] or "").strip()[:500]
            if "color" in changes and normalize_hex(changes["color"]):
                feed["color"] = normalize_hex(changes["color"])
            if feed.get("subject") and ("color" in changes or "subject" in changes):
                self.db.set_subject_color(feed["subject"], feed["color"])
        self._save(feeds)

    def remove_feed(self, feed_id: str) -> None:
        feeds = [feed for feed in self.list_feeds() if feed["id"] != feed_id]
        urls = self._urls()
        urls.pop(feed_id, None)
        self._save(feeds, urls)
        # Gli eventi del feed spariscono subito (sessioni e materiali restano).
        self.db.purge_unsynced_calendars(
            [calendar_id_for(feed["id"]) for feed in feeds], source=EVENT_SOURCE_ICAL
        )


def sync_ical_feeds(
    db: Database,
    store: ICalFeedStore,
    future_days: int = 365,
    fetch: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """Scarica tutti i feed, aggiorna gli eventi e applica la cancellazione speculare."""
    fetcher = fetch or default_fetch
    totals: dict[str, Any] = {"inserted": 0, "updated": 0, "removed": 0, "feeds": 0, "errors": []}
    feeds = [feed for feed in store.list_feeds() if feed["has_url"]]
    now = datetime.now()
    window_start = (now - timedelta(days=RECURRENCE_PAST_DAYS)).strftime(LOCAL_DATETIME_FMT)
    window_end = (now + timedelta(days=max(7, min(730, int(future_days))))).strftime(
        LOCAL_DATETIME_FMT
    )
    offline = 0
    for feed in feeds:
        label = feed["name"] or feed["url_hint"] or "Calendario iCal"
        try:
            data = fetcher(store.url(feed["id"]))
            parsed, warnings = parse_ics_bytes(data, label, horizon_days=future_days)
        except GoogleOfflineError:
            offline += 1
            continue
        except Exception as exc:
            message = str(exc) if isinstance(exc, ICalFeedError) else f"Calendario non leggibile: {exc}"
            message = mask_secrets(message)[:300]
            LOGGER.warning("Feed iCal '%s' non sincronizzato: %s", label, mask_secrets(message))
            store.update_feed(feed["id"], error=message)
            totals["errors"].append(f"{label}: {message}")
            continue
        if not feed["name"]:
            detected = calendar_name(data)
            if detected:
                store.update_feed(feed["id"], name=detected)
                label = detected
        calendar_id = calendar_id_for(feed["id"])
        events = []
        for event in parsed:
            events.append(
                {
                    **event,
                    "google_event_id": f"{event['uid']}|{event['start']}"[:400],
                    # Feed abbinato a una materia: vince la materia; altrimenti il
                    # fallback dell'import .ics (categorie, poi titolo).
                    "subject": feed["subject"] or event.get("subject") or event.get("title"),
                    "color_hex": feed["color"],
                    "color_id": None,
                }
            )
        result = db.upsert_google_events(
            calendar_id, events, source=EVENT_SOURCE_ICAL, source_file=label
        )
        removed = db.mirror_google_deletions(
            calendar_id,
            window_start,
            window_end,
            (event["google_event_id"] for event in events),
            source=EVENT_SOURCE_ICAL,
        )
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        store.update_feed(feed["id"], error="", last_ok=stamp)
        totals["inserted"] += result["inserted"]
        totals["updated"] += result["updated"]
        totals["removed"] += removed
        totals["feeds"] += 1
        if warnings:
            LOGGER.info("Feed iCal '%s': %d avvisi.", label, len(warnings))
    # Feed rimossi dalle Impostazioni: i loro eventi spariscono dall'agenda.
    totals["removed"] += db.purge_unsynced_calendars(
        [calendar_id_for(feed["id"]) for feed in store.list_feeds()],
        source=EVENT_SOURCE_ICAL,
    )
    if feeds and offline == len(feeds):
        raise GoogleOfflineError("Calendari iCal non raggiungibili.")
    return totals
