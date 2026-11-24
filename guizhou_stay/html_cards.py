from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from guizhou_stay import templates
from guizhou_stay.errors import invalid

COMPAT_CLASS = "stay-compat"


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    visible: bool
    children: list[Node | str] = field(default_factory=list)

    def content(self) -> str:
        if not self.visible:
            return ""
        return normalize("".join(child if isinstance(child, str) else child.content()
                                 for child in self.children))


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class TreeParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}
    HIDDEN = {"script", "style", "template", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document", {}, True)
        self.stack = [self.root]
        self.elements = 0

    def start(self, tag, attrs, *, closed):
        self.elements += 1
        if len(self.stack) > 80 or self.elements > 40_000:
            raise invalid("html_complexity", "页面嵌套或元素数超出采集限制")
        parent = self.stack[-1]
        props = {key: value or "" for key, value in attrs}
        style = props.get("style", "").casefold().replace(" ", "")
        visible = (
            parent.visible
            and tag not in self.HIDDEN
            and "hidden" not in props
            and props.get("aria-hidden", "").casefold() != "true"
            and "display:none" not in style
            and "visibility:hidden" not in style
        )
        node = Node(tag, props, visible)
        parent.children.append(node)
        if tag == "br" and visible:
            parent.children.append(" ")
        if not closed and tag not in self.VOID:
            self.stack.append(node)

    def handle_starttag(self, tag, attrs):
        self.start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self.start(tag, attrs, closed=True)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if self.stack[-1].visible:
            self.stack[-1].children.append(data)


def descendants(node: Node):
    for child in node.children:
        if isinstance(child, Node) and child.visible:
            yield child
            yield from descendants(child)


def has_class(node: Node, name: str) -> bool:
    return name in node.attrs.get("class", "").split()


def first(node: Node, *, tag: str | None = None, class_name: str | None = None) -> Node | None:
    for child in descendants(node):
        if (tag is None or child.tag == tag) and (class_name is None or has_class(child, class_name)):
            return child
    return None


def parse(html: str) -> Node:
    if len(html.encode("utf-8")) > 2_000_000:
        raise invalid("html_size", "页面超过单次解析大小限制")
    parser = TreeParser()
    parser.feed(html)
    parser.close()
    return parser.root


def text(node: Node, class_name: str) -> str | None:
    value = first(node, class_name=class_name)
    return value.content() or None if value else None


def reference(base: str, href: str) -> str:
    if not href:
        return ""
    resolved = urljoin(base, href.strip())
    try:
        value = urlsplit(resolved)
        if value.scheme in {"https", "http"} and value.hostname and not value.username:
            return resolved
    except ValueError:
        pass
    return ""


def card_fields(card: Node, position: int, base_url: str, *,
                template: str | None = None, alternate: bool = False) -> dict:
    link = first(card, tag="a")
    hours_text = text(card, "hours")
    window_attribute = card.attrs.get("data-window")
    window_code, basis = templates.interpret_window(template, window_attribute, hours_text)
    declared = card.attrs.get("data-item-id")
    item_id = card.attrs.get("data-id", "")
    if alternate and declared and item_id and declared != item_id:
        raise invalid("card_key", "兼容区卡片的记录键与 data-id 不一致",
                      {"position": position})
    return {
        "source_position": position,
        "item_id": item_id,
        "record_key": declared or item_id,
        "kind": card.attrs.get("data-kind", ""),
        "category": card.attrs.get("data-category", ""),
        "provider_id": card.attrs.get("data-provider", ""),
        "status": card.attrs.get("data-status", "active"),
        "updated_at": card.attrs.get("data-updated", ""),
        "title": text(card, "title") or (link.content() if link else ""),
        "href": reference(base_url, link.attrs.get("href", "")) if link else "",
        "region": text(card, "region") or "",
        "city": text(card, "city"),
        "address_hint": text(card, "address"),
        "price_band": text(card, "price"),
        "service_window": hours_text,
        "service_window_attribute": window_attribute,
        "window_code": window_code,
        "window_basis": basis,
        "template": template,
        "alternate": alternate,
        "text": card.content(),
    }


def card_signature(row: dict) -> dict:
    return {key: value for key, value in row.items()
            if key not in {"source_position", "alternate"}}


def collect(root: Node, base_url: str, card_class: str,
            *, template: str | None = None) -> list[dict]:
    nodes = [node for node in descendants(root) if has_class(node, card_class)]
    if len(nodes) > 500:
        raise invalid("card_limit", "页面的可见供给卡片超过采集限制")
    compat_regions = [node for node in descendants(root) if has_class(node, COMPAT_CLASS)]
    compat_nodes = [node for region in compat_regions for node in descendants(region)
                    if has_class(node, card_class)]
    compat_ids = {id(node) for node in compat_nodes}
    primary = [node for node in nodes if id(node) not in compat_ids]
    rows: list[dict] = []
    index: dict[str, int] = {}
    primary_signatures: dict[str, dict] = {}
    for position, node in enumerate(primary, start=1):
        row = card_fields(node, position, base_url, template=template)
        key = row["item_id"]
        if not key:
            rows.append(row)
            continue
        signature = card_signature(row)
        if key in primary_signatures:
            if primary_signatures[key] != signature:
                raise invalid("duplicate_card", "同一页面的同号卡片存在不同内容",
                              {"item_id": key, "position": position})
            continue
        primary_signatures[key] = signature
        index[key] = len(rows)
        rows.append(row)
    # 兼容区是同一记录的旧结构视图；按合作方记录键归并，绝不双计。
    for offset, node in enumerate(compat_nodes, start=len(primary) + 1):
        row = card_fields(node, offset, base_url, template=templates.LEGACY_TEMPLATE,
                          alternate=True)
        key = row["record_key"] or row["item_id"]
        if not key:
            rows.append(row)
            continue
        existing_index = index.get(key)
        if existing_index is None:
            row["compat_only"] = True
            index[key] = len(rows)
            rows.append(row)
            continue
        existing = rows[existing_index]
        if existing.get("alternate"):
            if card_signature(existing) != card_signature(row):
                raise invalid("duplicate_card", "兼容区的同号卡片存在不同内容",
                              {"item_id": key, "position": offset})
            continue
        rows[existing_index] = merge_view(existing, row, offset)
    return rows


def merge_view(existing: dict, alternate: dict, position: int) -> dict:
    """同一记录键的新版卡片与兼容区卡片合并，冲突与不可对照字段都保留。"""
    merged = templates.merge_views(existing, alternate)
    canonical = dict(merged["canonical"])
    alternate_view = dict(merged["alternate"])
    canonical["source_position"] = existing["source_position"]
    canonical["alternate"] = False
    canonical["record_key"] = existing["record_key"] or alternate["record_key"]
    canonical["conflicts"] = merged["conflicts"]
    canonical["alternate_interpretations"] = templates.interpretations([
        {"template": value.get("template"), "window_code": value.get("window_code")}
        for value in (existing, alternate)
    ])
    canonical["not_comparable_fields"] = merged["incomparable"]
    canonical["compat_view"] = {
        "template": alternate_view.get("template"),
        "window_code": alternate_view.get("window_code"),
        "service_window": alternate_view.get("service_window"),
        "fields": {key: alternate_view.get(key) for key in templates.VALUE_FIELDS},
        "source_position": alternate["source_position"],
    }
    return canonical


def metadata(root: Node) -> dict:
    title = first(root, tag="title")
    description = ""
    canonical = ""
    facts = {}
    for node in descendants(root):
        if node.tag == "meta":
            name = node.attrs.get("name", "").casefold()
            value = node.attrs.get("content", "")
            if name == "description":
                description = normalize(value)
            if name.startswith("stay-"):
                key = name.removeprefix("stay-")
                if key in facts:
                    raise invalid("duplicate_metadata", "同一采集标识在页面重复声明", {"name": name})
                facts[key] = value.strip()
        elif node.tag == "link" and node.attrs.get("rel", "").casefold() == "canonical":
            canonical = node.attrs.get("href", "")
    return {"title": title.content() if title else "", "description": description,
            "canonical": canonical, "capture": facts}


def parse_cards(html: str, base_url: str, *, card_class: str = "stay-card",
                template: str | None = None) -> list[dict]:
    return collect(parse(html), base_url, card_class, template=template)


def page_metadata(html: str) -> dict:
    return metadata(parse(html))


def card_stats(cards: list[dict]) -> dict:
    required = ("item_id", "title", "href", "region", "category", "provider_id", "kind", "updated_at")
    categories: dict[str, int] = {}
    regions: dict[str, int] = {}
    invalid_positions = []
    conflicted = []
    for card in cards:
        category = card["category"]
        region = card["region"]
        categories[category] = categories.get(category, 0) + 1
        regions[region] = regions.get(region, 0) + 1
        if any(not card[key] for key in required):
            invalid_positions.append(card["source_position"])
        if card.get("conflicts"):
            conflicted.append(card["item_id"])
    return {"count": len(cards), "categories": dict(sorted(categories.items())),
            "regions": dict(sorted(regions.items())), "invalid": invalid_positions,
            "conflicted": conflicted}


def extract_page(html: str, base_url: str, *, card_class: str = "stay-card",
                 template: str | None = None) -> dict:
    root = parse(html)
    page_meta = metadata(root)
    declared = page_meta["capture"].get("template") or template or templates.DEFAULT_TEMPLATE
    cards = collect(root, base_url, card_class, template=declared)
    return {"metadata": page_meta, "cards": cards, "stats": card_stats(cards),
            "template": declared}
