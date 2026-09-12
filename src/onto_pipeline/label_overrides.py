"""What language a label is written in, when the guess is not the last word
(PREP-NORMALIZE-LABELS-VERIFY/LANGUAGE-PRECEDENCE).

`terms.guess_language` decides on Spanish orthography and a short list of function words, so a
plain noun like `valor` or `tecnica` falls to the `en` default and the pair it forms with its
identifier is filed as a real divergence instead of an unverified translation. The model can
tell — it reads the words rather than the spelling — but normalization is a deterministic
function of the seed on disk, so an answer that is not an *input* of that function disappears on
the next run. Worse: a finding's id derives from its content, languages included, so a
correction applied without persisting does not reopen the same finding, it mints another one.

Hence a table, written from two sides and read by `normalize` through `LabelDecisions.languages`.

The row is keyed by the label text, not by the entity: the language is a property of the string,
ten classes can carry the same label, and the key then survives IRI reminting. If an accepted
typo changes the text the override simply stops applying and the level below takes over, which
is the right way round to fail. The cost is the homograph — `control`, `material`, `general` are
one language per session — and `und` plus a manual override is what covers it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .store import Store

USER = "user"
MODEL = "model"

# The three answers the language of a label can take. `und` is ISO 639-2's "undetermined": a
# proper name, an acronym, or a word spelled the same in both languages. It is never written
# into the graph — a literal needs a tag — but it is what stops an undetermined pair from being
# asserted as a same-language divergence.
UNDETERMINED = "und"
LANGUAGES = (UNDETERMINED, "en", "es")

SCHEMA = """
CREATE TABLE IF NOT EXISTS label_languages (
  session_id  TEXT NOT NULL,
  text        TEXT NOT NULL,      -- the label as it is compared, not the IRI
  language    TEXT NOT NULL,      -- es | en | und
  source      TEXT NOT NULL,      -- user | model
  created_at  TEXT,
  PRIMARY KEY (session_id, text)
);
"""


@dataclass(frozen=True)
class Override:
    language: str
    source: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: Store) -> None:
    conn.script(SCHEMA)
    conn.commit()


def load(conn: Store, *, session_id: str) -> dict[str, Override]:
    install(conn)
    return {
        row["text"]: Override(language=row["language"], source=row["source"])
        for row in conn.execute(
            "SELECT text, language, source FROM label_languages WHERE session_id = ?",
            (session_id,),
        )
    }


def record(
    conn: Store, languages: dict[str, str], *, source: str, session_id: str
) -> int:
    """The overrides, with `user` beating `model` and never the other way round.

    A correction made by hand replaces whatever is there. A model pass re-runs over the whole
    inventory every time, so it refreshes what it wrote before — but its delete is scoped to
    `source = 'model'`, and the insert then bounces off any user row that is still standing.
    """
    if source not in (USER, MODEL):
        raise ValueError(f"an override comes from the user or from the model, not {source!r}")
    unknown = sorted(set(languages.values()) - set(LANGUAGES))
    if unknown:
        raise ValueError(f"a label's language is one of {LANGUAGES}, not {unknown}")
    if not languages:
        return 0
    install(conn)
    if source == USER:
        conn.executemany(
            "DELETE FROM label_languages WHERE session_id = ? AND text = ?",
            [(session_id, text) for text in languages],
        )
    else:
        conn.executemany(
            "DELETE FROM label_languages WHERE session_id = ? AND text = ? AND source = ?",
            [(session_id, text, MODEL) for text in languages],
        )
    written = 0
    for text, language in languages.items():
        cursor = conn.execute(
            "INSERT INTO label_languages (session_id, text, language, source, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (session_id, text) DO NOTHING",
            (session_id, text, language, source, _now()),
        )
        written += cursor.rowcount == 1
    conn.commit()
    return written
