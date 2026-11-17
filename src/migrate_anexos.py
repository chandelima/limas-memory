"""Migração única: importa o registro antigo (data/anexos.json) para memory/anexos/*.md.

Preserva os IDs, para as referências `anexo:ID` que já estão na memória continuarem
valendo. Não sobrescreve anexos que já existem e não apaga o JSON antigo.

Uso:
    python src/migrate_anexos.py
    python src/migrate_anexos.py --json D:/outro/anexos.json --memory-dir D:/tmp/memoria
"""

import argparse
import json
import sys
from pathlib import Path

import config
from attachments.registry import Attachment, AttachmentRegistry
from storage.store import MemoryStore


def migrate(json_path: Path, registry: AttachmentRegistry) -> tuple[int, int]:
    """Devolve (importados, já existentes)."""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    imported = existing = 0
    for raw in data["attachments"]:
        item = Attachment(**raw)
        if registry.get(item.id) is not None:
            existing += 1
            continue
        registry.put(item)
        imported += 1
    return imported, existing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Importa data/anexos.json para memory/anexos/.")
    ap.add_argument("--json", type=Path, default=config.ROOT / "data" / "anexos.json")
    ap.add_argument("--memory-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.json.exists():
        print(f"Nada a migrar: {args.json} não existe.")
        return 0
    registry = AttachmentRegistry(MemoryStore(args.memory_dir or config.MEMORY_DIR))
    try:
        imported, existing = migrate(args.json, registry)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"Erro ao ler {args.json}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"{imported} anexo(s) importado(s), {existing} já existiam. O JSON antigo foi mantido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
