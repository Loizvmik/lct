"""Типографическая шкала — тесты дословно из брифа Task 4 (Step 1),
.superpowers/sdd/task-4-brief.md.

Одна вынужденная правка против буквального текста брифа: там три теста
обращаются к `profile_fixture(name)` внутри тела функции, не объявляя
фикстуру параметром — pytest подставляет фикстуры только через параметры
теста, без этого вызов падает с NameError. Добавлено как параметр;
имена тестов, докстроки и сами проверки — как в брифе.
"""
import io
import zipfile

from conftest import ALL_TEMPLATES

from deckforge.ooxml.geometry import Canvas
from deckforge.ooxml.package import PptxPackage
from deckforge.template.typography import _families_and_mono, _line_spacing
from deckforge.template.usage import FontUsage


def _package_from_slide_xml(slide_xml: str) -> PptxPackage:
    """Синтетический пакет из одного slide1.xml — тот же приём, что и в
    tests/template/test_grid.py (_package_from_slide_xml) и test_usage.py
    (_usage_from_slide_xml), для граничных случаев OOXML, которых нет ни в
    одном из трёх реальных учебных шаблонов."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="ppt/presentation.xml"/></Relationships>',
        )
        zf.writestr(
            "ppt/presentation.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
        )
        zf.writestr("ppt/slides/slide1.xml", slide_xml)
    buf.seek(0)
    return PptxPackage(zipfile.ZipFile(buf, "r"))


def test_scale_is_monotonic_and_covers_six_steps(profile_fixture):
    scale = profile_fixture("VK Tech шаблон.pptx").type_scale
    steps = [scale.steps[k] for k in ("micro", "caption", "body", "h2", "h1", "display")]
    assert steps == sorted(steps)
    assert scale.steps["body"] >= 12


def test_headline_line_spacing_is_tighter_than_body(profile_fixture):
    """Устойчивое правило во всех четырёх: заголовок 90%, body 100–110%."""
    for name in ALL_TEMPLATES:
        scale = profile_fixture(name).type_scale
        assert scale.heading_line_spacing <= scale.body_line_spacing


def test_no_more_than_two_families(profile_fixture):
    """Проверка аудита T01 «гарнитур больше двух» опирается на этот список."""
    for name in ALL_TEMPLATES:
        assert len(profile_fixture(name).type_scale.families) <= 2


def test_workspace_body_scale_falls_back_to_runs(profile_fixture):
    """У WorkSpace в лейаутах только title 36/54pt, body-шкалы нет вовсе —
    её нужно достроить из гистограммы run'ов, а не оставить пустой."""
    scale = profile_fixture("VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx").type_scale
    assert 12 <= scale.steps["body"] <= 20


# --- дополнительные тесты на утверждения, явно сформулированные текстом
# брифа (разведка, п.7-8), которых бриф не дал в виде готового кода ---

def test_arial_is_not_a_false_second_family_on_vktech(profile_fixture):
    """У VK Tech Arial встречается в 5 символах из 7586 (0.07% нетекста) —
    случайное вкрапление, не вторая гарнитура шаблона (нашёл general-purpose
    ревьюер: без порога по доле символов families = ['Play', 'Arial'], хотя
    Arial практически не используется)."""
    scale = profile_fixture("VK Tech шаблон.pptx").type_scale
    assert scale.families == ["Play"]
    assert scale.families_total == 1


def test_mono_fonts_are_excluded_from_families(profile_fixture):
    """Consolas (526 симв. у VK Tech, 262 у Education) — код, не вторая
    гарнитура бренда: должен уйти в `mono`, не в `families` (иначе аудит
    «гарнитур больше двух» будет ложно срабатывать — брифом, п.8)."""
    for name in ("VK Tech шаблон.pptx", "Шаблон презентации VK Education.pptx"):
        scale = profile_fixture(name).type_scale
        assert "Consolas" not in scale.families
        assert "Consolas" in scale.mono


def test_bold_and_italic_are_not_idiomatic_in_these_templates(profile_fixture):
    """Курсива нет нигде, жирный — единицы run'ов из сотен: генератор,
    ставящий bold/italic «по умолчанию», выйдет из шаблона (брифом, п.7)."""
    for name in ALL_TEMPLATES:
        scale = profile_fixture(name).type_scale
        assert scale.bold_is_idiomatic is False
        assert scale.italic_is_idiomatic is False


def test_every_step_carries_a_confidence_score(profile_fixture):
    """«Каждое число, которое ты выдаёшь наружу, должно нести меру
    уверенности» — общее требование задачи, не только для Grid."""
    scale = profile_fixture("VK Tech шаблон.pptx").type_scale
    for key in ("micro", "caption", "body", "h2", "h1", "display"):
        assert key in scale.step_confidence
        assert 0.0 <= scale.step_confidence[key] <= 1.0


# --- находки повторного код-ревью (п.4: уверенность интерлиньяжа, п.5:
# нормализация имён гарнитур) ---

_SLIDE_WITHOUT_ANY_LNSPC = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Body"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr/>
        <p:txBody>
          <a:p><a:pPr/><a:r><a:rPr sz="1800"/><a:t>Текст без a:lnSpc</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def test_line_spacing_confidence_is_zero_when_not_measured():
    """Находка повторного код-ревью (п.4): на незнакомом шаблоне без
    абзацных a:lnSpc heading_line_spacing/body_line_spacing тихо
    откатываются на константы 0.9/1.0 — аудит обязан увидеть явный признак
    "это не измерение", а не спутать константу с честным замером. На
    контрольном ЛЦТ2026 title_sp пуст именно так (см. отчёт) — эта
    синтетика воспроизводит тот же случай независимо от файла."""
    pkg = _package_from_slide_xml(_SLIDE_WITHOUT_ANY_LNSPC)
    canvas = Canvas(width_emu=12192000, height_emu=6858000)
    heading, heading_conf, body, body_conf = _line_spacing(pkg, canvas)
    assert heading == 0.9
    assert body == 1.0
    assert heading_conf == 0.0
    assert body_conf == 0.0


def test_line_spacing_confidence_is_positive_when_measured(profile_fixture):
    """VK Tech реально задаёт a:lnSpc и в title-, и в прочих плейсхолдерах
    (см. отчёт разведки) — на измеренном значении confidence обязан быть
    положительным, отличимым от фолбэка на константу."""
    scale = profile_fixture("VK Tech шаблон.pptx").type_scale
    assert scale.heading_line_spacing_confidence > 0.0
    assert scale.body_line_spacing_confidence > 0.0


def test_family_variants_are_normalized_by_stripping_style_suffix():
    """Находка повторного код-ревью (п.5): начертание в имени гарнитуры
    ("Montserrat Medium") раздувает счётчик семейств — на контрольном
    ЛЦТ2026 families_total было 3 вместо честных 2 (Montserrat и
    Montserrat Medium считались разными гарнитурами). Синтетика с
    известным правильным ответом: одна гарнитура в трёх начертаниях
    (Regular/Bold/Italic) должна схлопнуться в одно нормализованное имя,
    а не раздувать families_total."""
    fonts = {
        "Acme Sans": FontUsage(family="Acme Sans", chars=500),
        "Acme Sans Bold": FontUsage(family="Acme Sans Bold", chars=300),
        "Acme Sans Italic": FontUsage(family="Acme Sans Italic", chars=100),
        "Consolas Bold": FontUsage(family="Consolas Bold", chars=50),
    }
    families, mono, families_total, family_variants = _families_and_mono(fonts)
    assert families == ["Acme Sans"]
    assert families_total == 1
    assert family_variants["Acme Sans"] == ["Acme Sans", "Acme Sans Bold", "Acme Sans Italic"]
    assert mono == ["Consolas"]


def test_family_variants_preserves_raw_names_used_for_layout():
    """«Наружу должны идти и нормализованное семейство, и фактические
    имена, которыми оно набрано» — без стилевой связки .pptx ссылается на
    начертание буквально по имени (например rPr@b="1" сам по себе не
    гарантирует, что в файле есть жирное начертание с ИМЕНЕМ "X Bold" —
    вёрстке нужно знать точное имя, которым набирался жирный текст)."""
    fonts = {
        "Open Sans": FontUsage(family="Open Sans", chars=900),
        "Open Sans SemiBold": FontUsage(family="Open Sans SemiBold", chars=200),
    }
    _, _, _, family_variants = _families_and_mono(fonts)
    assert "Open Sans SemiBold" in family_variants["Open Sans"]
    assert "Open Sans" in family_variants["Open Sans"]


def test_families_total_reflects_normalized_control_file_scenario():
    """Контрольный ЛЦТ2026 (не в тестах, но описанная находка): fonts =
    {'Montserrat': 340, 'Poppins Light': 26, 'Montserrat Medium': 47} давал
    families_total=3 при двух реальных гарнитурах. Воспроизводим теми же
    числами, независимо от чтения файла — известный правильный ответ: 2."""
    fonts = {
        "Montserrat": FontUsage(family="Montserrat", chars=340),
        "Poppins Light": FontUsage(family="Poppins Light", chars=26),
        "Montserrat Medium": FontUsage(family="Montserrat Medium", chars=47),
    }
    families, mono, families_total, family_variants = _families_and_mono(fonts)
    assert families_total == 2
    assert families == ["Montserrat", "Poppins"]
    assert family_variants["Montserrat"] == ["Montserrat", "Montserrat Medium"]
