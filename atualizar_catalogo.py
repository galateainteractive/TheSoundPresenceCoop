"""Sincroniza o Coop com o catálogo, as composições e os grooves do Unity.

Uso: python atualizar_catalogo.py [--unity-project CAMINHO] [--check]
"""

import argparse
import filecmp
import json
import re
import shutil
import sys
from pathlib import Path


COOP_ROOT = Path(__file__).resolve().parent
DEFAULT_UNITY_PROJECT = COOP_ROOT.parents[1] / "VejaOSomGit" / "vejaosom"
COMPOSITIONS = Path("Assets/StreamingAssets/Compositions/_MODULES")
GROOVES = Path("Assets/Resources/Grooves")
CATALOG = Path("Assets/Resources/InterpreterLevelCatalog.asset")


def scalar(value):
    value = value.strip()
    if value.startswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def component(value):
    if not value or value in (".", "..") or re.search(r'[\\/:*?"<>|]', value):
        raise ValueError(f"Nome de pasta inválido no catálogo ou settings: {value!r}")
    return value


def read_catalog(path):
    """Lê os campos necessários do ScriptableObject YAML salvo pelo Unity."""
    modules = []
    module = level = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"  - folderName: (.*)", line)
        if match:
            module = {"folderName": scalar(match.group(1)), "levels": []}
            modules.append(module)
            level = None
            continue
        match = re.fullmatch(r"    - folderName: (.*)", line)
        if match:
            if module is None:
                raise ValueError("Nível encontrado antes do módulo")
            level = {"folderName": scalar(match.group(1))}
            module["levels"].append(level)
            continue
        match = re.fullmatch(r"(    |      )(displayName|visible):\s*(.*)", line)
        if match:
            target = module if len(match.group(1)) == 4 else level
            if target is None:
                raise ValueError(f"Campo fora do módulo ou nível: {line}")
            target[match.group(2)] = scalar(match.group(3))

    if not modules:
        raise ValueError(f"Nenhum módulo encontrado em {path}")
    selected = []
    seen = set()
    for module in modules:
        module_name = component(module["folderName"])
        if module.get("visible") not in ("0", "1"):
            raise ValueError(f"Visibilidade ausente/inválida: {module_name}")
        for level in module["levels"]:
            folder = component(level["folderName"])
            if level.get("visible") not in ("0", "1") or not level.get("displayName"):
                raise ValueError(f"Nome ou visibilidade ausente/inválida: {module_name}/{folder}")
            if module["visible"] == "1" and level["visible"] == "1":
                if folder.casefold() in seen:
                    raise ValueError(f"Pasta de composição repetida entre módulos: {folder}")
                seen.add(folder.casefold())
                selected.append((module_name, module.get("displayName") or module_name, level))
    return selected


def build_plan(project):
    selected = read_catalog(project / CATALOG)
    source_root = project / COMPOSITIONS
    groove_root = project / GROOVES
    audio_by_name = {}
    for audio in groove_root.rglob("*.wav"):
        audio_by_name.setdefault(audio.stem.casefold(), []).append(audio)
    existing_groove_folders = {}
    for directory in (COOP_ROOT / "grooves").iterdir():
        if directory.is_dir():
            key = directory.name.casefold()
            if key in existing_groove_folders:
                raise ValueError(f"Pastas de groove com nomes ambíguos: {directory}")
            existing_groove_folders[key] = directory.name

    files = {}
    groove_files = {}
    catalog = []
    for module_folder, module_display, level in selected:
        folder = level["folderName"]
        source = source_root / module_folder / folder
        if not source.is_dir():
            print(f"Aviso: composição selecionada ausente no Unity: {source}", file=sys.stderr)
            continue
        settings_path = source / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        clips = settings.get("grooveClipNames")
        set_name = component(settings.get("sourceGrooveSetName"))
        groove_folder = existing_groove_folders.get(set_name.casefold(), set_name)
        if not isinstance(clips, list) or not clips or settings.get("numberOfGrooves") != len(clips):
            raise ValueError(f"Lista de grooves inconsistente: {source}")
        source_files = [p for p in source.iterdir() if p.is_file() and p.suffix.lower() in (".json", ".wav", ".sin")]
        if not source_files:
            raise ValueError(f"Composição sem arquivos: {source}")
        for session in settings.get("dashboardSessions", []):
            index = session["grooveIndex"]
            if not isinstance(index, int) or index < 0 or index >= len(clips):
                raise ValueError(f"Índice de groove inválido em {source}: {index}")
            for track in session.get("tracks", []):
                if not (source / f"clip_{track['globalId']}.wav").exists():
                    raise ValueError(f"Áudio de track ausente em {source}: {track['globalId']}")
        for item in source_files:
            files[COOP_ROOT / "musicas" / folder / item.name] = item
        for index, clip in enumerate(clips):
            matches = audio_by_name.get(str(clip).casefold(), [])
            if len(matches) != 1:
                raise ValueError(f"Groove {clip!r}: esperado 1 WAV, encontrados {len(matches)}")
            destination = COOP_ROOT / "grooves" / groove_folder / f"{index}.wav"
            previous = groove_files.get(destination)
            if previous and previous != matches[0]:
                raise ValueError(f"Variação conflitante para {destination}")
            groove_files[destination] = matches[0]
        catalog.append({"nome": level["displayName"], "pasta": f"musicas/{folder}", "modulo": module_display, "grooveFolder": groove_folder})

    files.update(groove_files)
    catalog_bytes = (json.dumps(catalog, ensure_ascii=False, indent=4) + "\n").encode("utf-8")
    return files, catalog_bytes, len(catalog), len(groove_files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unity-project", type=Path, default=DEFAULT_UNITY_PROJECT)
    parser.add_argument("--check", action="store_true", help="Confere sem modificar arquivos")
    args = parser.parse_args()
    project = args.unity_project.resolve()
    files, catalog_bytes, composition_count, groove_count = build_plan(project)
    catalog_path = COOP_ROOT / "composicoes.json"
    changed = [dst for dst, src in files.items() if not dst.exists() or not filecmp.cmp(src, dst, shallow=False)]
    catalog_changed = not catalog_path.exists() or catalog_path.read_bytes() != catalog_bytes
    previous = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else []
    current_folders = {entry["pasta"] for entry in json.loads(catalog_bytes)}
    stale_folders = []
    music_root = (COOP_ROOT / "musicas").resolve()
    for entry in previous:
        folder = Path(entry["pasta"])
        if len(folder.parts) != 2 or folder.parts[0] != "musicas":
            raise ValueError(f"Pasta inválida no catálogo anterior: {folder}")
        if entry["pasta"] in current_folders:
            continue
        destination = COOP_ROOT / folder
        if destination.is_symlink() or destination.resolve().parent != music_root:
            raise ValueError(f"Pasta fora de músicas: {destination}")
        if destination.is_dir():
            stale_folders.append(destination)
    print(f"Selecionadas: {composition_count} composições; {groove_count} variações de groove")
    print(f"Atualizar/copiar: {len(changed)} arquivos; catálogo: {'sim' if catalog_changed else 'não'}")
    print(f"Remover: {len(stale_folders)} pastas")
    if args.check:
        return int(bool(changed or stale_folders or catalog_changed))

    for destination in changed:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files[destination], destination)
    for destination in stale_folders:
        shutil.rmtree(destination)
    if catalog_changed:
        catalog_path.write_bytes(catalog_bytes)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        sys.exit(2)
