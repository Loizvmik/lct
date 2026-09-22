"""Реестр версий агентов/конфигов и его встраивание в готовый .pptx (Task 15).

ТЗ п.2.4 «Версионирование скиллов и агентов, лежащих в основе воркфлоу» и
п.4 «Промпты/конфиги... лежат отдельными файлами». Четыре проверки:
- у каждого агента и конфига есть версия в понятном (semver) формате;
- правка промпта меняет отпечаток записи в реестре, даже без ручного bump'а
  версии;
- по собранному .pptx видно, какими версиями он собран (пользовательское
  свойство документа `deckforge_workflow`);
- ('workflow' не тянет 'compose' на верхнем уровне модуля — build_deck
  импортируется только внутри теста, который его использует, дешёвые тесты
  реестра не платят за это дороже).
"""
from __future__ import annotations
from pathlib import Path

from deckforge.workflow.versions import AGENTS_DIR, CONFIG_FILES, VERSION_RE, manifest


def test_every_agent_and_config_declares_a_version():
    entries = manifest().entries
    assert entries, "реестр версий пуст — ни одного агента/конфига не найдено"
    for entry in entries:
        assert VERSION_RE.match(entry.version), (
            f"{entry.kind} {entry.name} ({entry.path}): версия {entry.version!r} "
            "не в формате major.minor.patch"
        )


def test_manifest_lists_every_agent_and_every_configured_config_file():
    names = {e.name for e in manifest().entries}
    for agent_dir in sorted(p for p in AGENTS_DIR.iterdir() if p.is_dir()):
        agent_md = agent_dir / "AGENT.md"
        if agent_md.exists():
            assert agent_dir.name in names, f"агент {agent_dir.name} не попал в реестр"
    for config_path in CONFIG_FILES:
        assert config_path.name in names, f"конфиг {config_path.name} не попал в реестр"


def test_manifest_hashes_change_when_a_prompt_changes():
    before = manifest()
    path = Path("agents/outline-writer/AGENT.md")
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(original + "\nДополнительное правило.\n", encoding="utf-8")
        after = manifest()
        assert after.sha_for("outline-writer") != before.sha_for("outline-writer")
        # Версия во frontmatter не тронута — отпечаток всё равно обязан
        # разойтись, ровно за этим он и существует (правка без bump'а версии
        # не должна остаться незамеченной).
        assert after.version_for("outline-writer") == before.version_for("outline-writer")
    finally:
        path.write_text(original, encoding="utf-8")
    restored = manifest()
    assert restored.sha_for("outline-writer") == before.sha_for("outline-writer")


def test_manifest_hash_changes_when_a_config_threshold_changes():
    before = manifest()
    path = Path("config/audit.yaml")
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(original + "\n# правка порога для теста\n", encoding="utf-8")
        after = manifest()
        assert after.sha_for("audit.yaml") != before.sha_for("audit.yaml")
    finally:
        path.write_text(original, encoding="utf-8")


def test_as_property_value_lists_every_entry_as_name_and_version():
    value = manifest().as_property_value()
    for entry in manifest().entries:
        assert entry.label() in value


def test_manifest_is_written_into_every_generated_deck():
    """По готовому файлу должно быть видно, какими версиями воркфлоу он собран."""
    from deckforge.compose.builder import Variant, build_deck
    from deckforge.ooxml.customprops import read_custom_properties
    from deckforge.plan.spec import DeckSpec, SlideSpec
    from deckforge.template.profile import TemplateProfile

    template = Path("dataset/templates/VK Tech шаблон.pptx")
    profile = TemplateProfile.from_file(template)
    spec = DeckSpec(
        title="Тест реестра версий",
        language="ru",
        slides=[SlideSpec(index=0, kind="section", headline="Версия воркфлоу в файле")],
    )

    out_path = build_deck(spec, profile, template, Variant.dense)
    props = read_custom_properties(out_path)

    assert "deckforge_workflow" in props
    expected = manifest()
    assert expected.entry("outline-writer").label() in props["deckforge_workflow"]
    assert props["deckforge_workflow"] == expected.as_property_value()
