"""Anteprima del JSON incompleto e modifiche manuali separate dall'output validato."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import from_json

from codice_tesi.discharge.provenance import draft_json_text
from codice_tesi.discharge.template import LetterTemplate


class EditorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    sections: dict[str, Annotated[str, Field(max_length=100000)]] = Field(max_length=32)


def preview_sections(raw: str, template: LetterTemplate) -> list[dict]:
    """Decodifica anche l'ultima stringa aperta; non certifica testo o citazioni.

    Si accetta solo il prefisso ordinato delle sezioni canoniche, senza interpretare
    preamboli, chiamate di funzione, fonti o ragionamento come testo della lettera.
    """
    try:
        data = from_json(
            draft_json_text(raw, partial=True),
            allow_partial="trailing-strings",
            cache_strings=False,
        )
    except ValueError:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        return []
    sections = []
    for item, definition in zip(data["sections"], template.sections):
        if not isinstance(item, dict) or item.get("section_id") != definition.id:
            break
        if not isinstance(item.get("text"), str):
            break
        sections.append({"section_id": definition.id, "text": item["text"]})
    return sections
