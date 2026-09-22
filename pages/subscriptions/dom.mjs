// Attribute names and tags are code-owned; external values are never parsed as HTML.
export function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value !== false && value != null) node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child != null && child !== false) node.append(child);
  }
  return node;
}

export function emptyState(title, description, className = "detail-empty") {
  return element("div", { class: className },
    element("strong", {}, title), element("span", {}, description));
}
