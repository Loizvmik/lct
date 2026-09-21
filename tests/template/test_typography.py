"""Типографическая шкала — тесты дословно из брифа Task 4 (Step 1),
.superpowers/sdd/task-4-brief.md.

Одна вынужденная правка против буквального текста брифа: там три теста
обращаются к `profile_fixture(name)` внутри тела функции, не объявляя
фикстуру параметром — pytest подставляет фикстуры только через параметры
теста, без этого вызов падает с NameError. Добавлено как параметр;
имена тестов, докстроки и сами проверки — как в брифе.
"""
from conftest import ALL_TEMPLATES


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
