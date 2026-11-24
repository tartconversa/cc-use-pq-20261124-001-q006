from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from guizhou_stay.errors import invalid

DEFAULT_TEMPLATE = "directory-v1"
LEGACY_TEMPLATE = "directory-v1"

# 服务时段的口径。缺字段、明确全天、待确认与旧版笼统的“可预约”必须互相区分。
WindowCode = str

# 可用于跨模板比较的业务字段。service_window 的“口径”随模板不同，不能跨口径比较。
VALUE_FIELDS = ("kind", "title", "provider_id", "city", "region", "category",
                "status", "updated_at", "address_hint", "price_band",
                "service_window", "tags")


@dataclass(frozen=True)
class Profile:
    id: str
    version: int
    label: str
    structured_window: bool

    def caliber(self, field_name: str) -> str:
        if field_name == "service_window":
            return "structured-window" if self.structured_window else "legacy-window"
        return "directory"


PROFILES = {
    "directory-v1": Profile("directory-v1", 1, "旧版目录", structured_window=False),
    "directory-v2": Profile("directory-v2", 2, "新版目录", structured_window=True),
}


def profile(name: str | None) -> Profile:
    key = name or DEFAULT_TEMPLATE
    value = PROFILES.get(key)
    if value is None:
        raise invalid("unknown_template", "来源声明的模板版本未登记",
                      {"template": key, "known": sorted(PROFILES)})
    return value


def known(name: str | None) -> bool:
    return (name or DEFAULT_TEMPLATE) in PROFILES


def version(name: str | None) -> int:
    return 0 if name is None else profile(name).version


def caliber(name: str | None, field_name: str) -> str:
    if name is None:
        # 人工结构化记录没有模板口径，逐字段按值比较。
        return "manual"
    return profile(name).caliber(field_name)


def comparable(name_a: str | None, name_b: str | None) -> set[str]:
    """两个模板都能按同一口径解释的字段。"""
    return {key for key in VALUE_FIELDS if caliber(name_a, key) == caliber(name_b, key)}


def incomparable(name_a: str | None, name_b: str | None) -> list[str]:
    if name_a is None and name_b is None:
        return []
    if name_a is None or name_b is None:
        return []
    return sorted(set(VALUE_FIELDS) - comparable(name_a, name_b))


def interpret_window(template: str | None, attribute: str | None,
                     text_value: str | None) -> tuple[WindowCode, str]:
    """返回 (时段口径, 解释依据)。

    只沿卡片所属模板的有效口径解释：
    - 新版使用显式 data-window；缺失即 absent，不从文案猜测。
    - 旧版无法区分全天与夜间值班，任何时段文案都只表示“可预约”。
    """
    value = (attribute or "").strip().casefold()
    hours = (text_value or "").strip()
    if template == "directory-v1" or template is None:
        if not hours and not value:
            return "absent", "directory-v1:no-window-field"
        return "unspecified-bookable", "directory-v1:legacy-bookable"
    if not profile(template).structured_window:  # 其他旧口径
        return ("unspecified-bookable" if hours or value else "absent",
                f"{template}:legacy-bookable")
    if not value:
        # 显式声明优先级最高；没有声明就是缺失，绝不从“全天接待”之类文案反推。
        return "absent", f"{template}:no-window-field"
    mapping = {
        "all-day": "all-day", "all_day": "all-day", "allday": "all-day",
        "night-duty": "night-duty", "night_duty": "night-duty",
        "night-only": "night-duty",
        "pending": "pending", "confirming": "pending",
    }
    if value in mapping:
        return mapping[value], f"{template}:data-window={mapping[value]}"
    raise invalid("window_code", "新版卡片声明了无法识别的服务时段",
                  {"template": template, "value": attribute})


def interpretations(values: Iterable[dict]) -> list[dict]:
    index = {}
    for value in values:
        key = (value.get("template"), value.get("window_code"))
        index[key] = {"template": key[0], "window_code": key[1]}
    return [index[key] for key in sorted(index, key=lambda item: (item[0] or "", item[1] or ""))]


def merge_views(left: dict, right: dict) -> dict:
    """比较同一记录键下的两个模板视图。

    ``left``/``right`` 是含 ``template``、``window_code`` 与业务字段的字典。
    返回冲突、不可对照字段以及应作为规范的视图。
    """
    template_a = left.get("template")
    template_b = right.get("template")
    if template_a == template_b:
        shared = set(VALUE_FIELDS)
        unable: list[str] = []
    else:
        unable = incomparable(template_a, template_b)
        shared = comparable(template_a, template_b)
    conflicts = {}
    for field_name in sorted(shared):
        first, second = left.get(field_name), right.get(field_name)
        if first != second:
            conflicts[field_name] = {
                "sides": {template_a or "manual": first, template_b or "manual": second}
            }
    if version(template_b) > version(template_a):
        canonical, other = right, left
    else:
        canonical, other = left, right
    return {
        "conflicts": conflicts,
        "incomparable": unable,
        "canonical": canonical,
        "alternate": other,
        "templates": sorted({value for value in (template_a, template_b) if value}),
    }
