"""Tests for ``Extractor`` — schema parsing + cost computation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agency_brain.agents.crm_updater.extractor import (
    DEFAULT_FLASH_MODEL,
    Extractor,
    ExtractorConfig,
)
from agency_brain.agents.crm_updater.models import GmailMessage


@dataclass
class _Resp:
    text: str
    prompt_tokens: int = 1500
    output_tokens: int = 300

    @property
    def usage_metadata(self) -> Any:
        outer = self

        class _U:
            prompt_token_count = outer.prompt_tokens
            candidates_token_count = outer.output_tokens

        return _U()


class _StubModels:
    def __init__(self, *, response: _Resp) -> None:
        self.response = response
        self.calls: list[tuple[str, Any]] = []

    def generate_content(self, *, model: str, contents, config) -> _Resp:
        self.calls.append((model, contents))
        return self.response


class _StubClient:
    def __init__(self, *, models: _StubModels) -> None:
        self.models = models


def _msg(body: str = "Following up on the Q2 review.") -> GmailMessage:
    return GmailMessage(
        message_id="m1",
        thread_id="t1",
        subject="Re: Q2 review",
        from_addr="sarah@example.com",
        to_addrs=("owner@example.com",),
        cc_addrs=(),
        body_text=body,
        received_at=datetime.now(UTC),
    )


def _make_extractor(*, response: _Resp) -> tuple[Extractor, _StubModels]:
    models = _StubModels(response=response)
    client = _StubClient(models=models)
    return Extractor(ExtractorConfig(project_id="p"), client=client), models


def test_extract_parses_full_response() -> None:
    payload = {
        "extracted_tasks": [
            {
                "title": "Send Q2 proposal to Acme",
                "due_date": "2026-05-15",
                "linked_account_name": "Acme Corp",
                "linked_contact_email": "sarah@example.com",
                "confidence": 0.85,
            }
        ],
        "contact_updates": [
            {
                "contact_email": "sarah@example.com",
                "last_contact_date": "2026-05-09",
                "next_followup_suggested": "2026-05-23",
                "warmth_change": "warmer",
                "context_note": "Asked about Q2 review timeline.",
            }
        ],
        "account_mentions": [
            {
                "account_name": "Acme Corp",
                "context_note": "Q2 review in progress.",
                "new_contacts": [],
            }
        ],
    }
    extractor, models = _make_extractor(response=_Resp(text=json.dumps(payload)))
    result = extractor.extract(message=_msg())
    assert len(result.extraction.extracted_tasks) == 1
    assert result.extraction.extracted_tasks[0].title == "Send Q2 proposal to Acme"
    assert len(result.extraction.contact_updates) == 1
    assert result.extraction.contact_updates[0].warmth_change == "warmer"
    assert len(result.extraction.account_mentions) == 1
    assert [m for m, _ in models.calls] == [DEFAULT_FLASH_MODEL]


def test_extract_returns_empty_arrays_on_unparseable_json() -> None:
    extractor, _ = _make_extractor(response=_Resp(text="not json at all"))
    result = extractor.extract(message=_msg())
    assert result.extraction.extracted_tasks == ()
    assert result.extraction.contact_updates == ()
    assert result.extraction.account_mentions == ()
    assert result.confidence == 0.0


def test_extract_skips_tasks_with_blank_title() -> None:
    payload = {
        "extracted_tasks": [
            {"title": "Real task", "confidence": 0.9},
            {"title": "", "confidence": 0.5},
            {"title": None, "confidence": 0.5},
        ],
        "contact_updates": [],
        "account_mentions": [],
    }
    extractor, _ = _make_extractor(response=_Resp(text=json.dumps(payload)))
    result = extractor.extract(message=_msg())
    titles = [t.title for t in result.extraction.extracted_tasks]
    assert titles == ["Real task"]


def test_extract_aggregates_task_confidence() -> None:
    payload = {
        "extracted_tasks": [
            {"title": "A", "confidence": 0.8},
            {"title": "B", "confidence": 0.4},
        ],
        "contact_updates": [],
        "account_mentions": [],
    }
    extractor, _ = _make_extractor(response=_Resp(text=json.dumps(payload)))
    result = extractor.extract(message=_msg())
    assert abs(result.confidence - 0.6) < 1e-9


def test_extract_computes_cost_from_token_counts() -> None:
    extractor, _ = _make_extractor(
        response=_Resp(
            text=json.dumps({"extracted_tasks": [], "contact_updates": [], "account_mentions": []}),
            prompt_tokens=2000,
            output_tokens=400,
        )
    )
    result = extractor.extract(message=_msg())
    expected = (2000 / 1_000_000.0) * 0.30 + (400 / 1_000_000.0) * 2.50
    assert abs(result.cost_usd - expected) < 1e-9


def test_extract_passes_known_lists_into_prompt() -> None:
    """The known account / contact lists must reach the prompt — this is
    how the model honors the "must match KNOWN ACCOUNTS / CONTACTS" rule."""
    extractor, models = _make_extractor(
        response=_Resp(
            text=json.dumps({"extracted_tasks": [], "contact_updates": [], "account_mentions": []})
        )
    )
    extractor.extract(
        message=_msg(),
        known_account_names=("Acme Corp", "Beta Co"),
        known_contact_emails=("sarah@example.com",),
    )
    sent = models.calls[0][1][0]["parts"][0]["text"]
    assert "Acme Corp" in sent
    assert "Beta Co" in sent
    assert "sarah@example.com" in sent
