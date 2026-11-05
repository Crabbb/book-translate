// Book template. Geometry and colours are the measured values of the source design (ir/style).
// fallback: false - a missing glyph must not silently use another font.

#let SERIF = "Libertinus Serif"
#let SANS = "Source Sans 3"
#let MONO = "Liberation Mono"
#let K(v) = cmyk(0%, 0%, 0%, v)
#let ink = K(100%)
#let codeink = cmyk(75%, 68%, 67%, 90%)
#let band = K(75%)

// Marks: where each unit landed, read back by the artifact checks.
#let mark(id) = [#metadata(id)<unit>#metadata(id)#label("ix-" + id)]
#let bodymark = [#metadata("b")<bodymark>]
#let srcpage(n) = [#metadata(n)#label("src-" + str(n))]

#let pnum(loc) = numbering("1", ..counter(page).at(loc))
#let pageref(l) = context { let loc = locate(l); link(loc, pnum(loc)) }
// Index references. Numbers exist only after layout, so repeats are removed here: two source
// pages can land on one new page. A repeated table header repeats its marks: first one counts.
#let pagerefs(..groups) = context {
  let seen = ()
  let parts = ()
  for group in groups.pos() {
    let first = query(group.first()).first().location()
    let last = query(group.last()).first().location()
    if first.page() == last.page() {
      if first.page() not in seen {
        seen.push(first.page())
        parts.push(link(first, pnum(first)))
      }
    } else {
      seen.push(first.page())
      seen.push(last.page())
      parts.push([#link(first, pnum(first))–#link(last, pnum(last))])
    }
  }
  parts.join(", ")
}

#let mono(s, url: none, bold: false) = {
  // 9 pt: Liberation Mono looks larger than Courier at the same size.
  let t = text(font: MONO, size: 9pt, hyphenate: false, weight: if bold { 700 } else { 400 }, s)
  if url != none { link(url, t) } else { t }
}

#let running = context {
  let pg = here().page()
  if query(heading.where(level: 1)).any(h => h.location().page() == pg) { return }
  // A page with no text at all is a blank; a page of only a table still has its marks.
  if not query(<bodymark>).any(m => m.location().page() == pg) and not query(<unit>).any(m => m.location().page() == pg) { return }
  let n = counter(page).get().first()
  let chaps = query(selector(heading.where(level: 1)).before(here()))
  if chaps.len() == 0 { return }
  let secs = query(heading.where(level: 2))
  let on = secs.filter(h => h.location().page() == pg)
  // A section of an earlier chapter does not name this one: the index has no sections of its own.
  let before = query(selector(heading.where(level: 2)).before(here())).filter(h => h.location().page() >= chaps.last().location().page())
  let cur = if on.len() > 0 { on.first().body } else if before.len() > 0 { before.last().body } else { chaps.last().body }
  set text(font: SANS, size: 9pt, fill: ink, hyphenate: false)
  place(top + left, dx: 72pt, dy: 51.2pt, line(length: 396pt, stroke: .5pt + ink))
  if calc.odd(n) {
    place(top + left, dx: 72pt, dy: 34.5pt, box(width: 396pt, align(right, cur)))
    place(top + left, dx: 486pt, dy: 34.5pt, str(n))
  } else {
    place(top + left, dx: 72pt, dy: 34.5pt, box(width: 396pt, chaps.last().body))
    place(top + left, dx: 0pt, dy: 34.5pt, box(width: 54pt, align(right, str(n))))
  }
}

#let book(lang: "en", title: "", body) = {
  set document(title: title)
  set page(width: 540pt, height: 666pt, margin: (inside: 72pt, outside: 72pt, top: 67.3pt, bottom: 42pt), foreground: running)
  set text(font: SERIF, size: 10pt, lang: lang, hyphenate: true, fill: ink, top-edge: .7em, bottom-edge: -.3em, fallback: false)
  set par(justify: true, leading: 2.8pt, spacing: 9.9pt, linebreaks: "optimized")
  set smartquote(enabled: false)
  set strong(delta: 300)
  show heading: set text(font: SANS, hyphenate: false, fill: ink)
  show heading.where(level: 1): it => block(above: 0pt, below: 31.1pt, width: 100%, {
    set align(right)
    set par(justify: false, leading: 10pt)
    set text(size: 25pt, weight: 700, tracking: .06em)
    it.body
  })
  show heading.where(level: 2): it => block(above: 17.75pt, below: 10.45pt, sticky: true, {
    set par(justify: false, leading: 6.5pt); set text(size: 14.5pt, weight: 600, tracking: .03em); it.body })
  show heading.where(level: 3): it => block(above: 16.89pt, below: 11.11pt, sticky: true, {
    set par(justify: false, leading: 6.5pt); set text(size: 12.3pt, weight: 600, tracking: .02em); it.body })
  show heading.where(level: 4): it => block(above: 12.44pt, below: 11.56pt, sticky: true, {
    set par(justify: false, leading: 4pt); set text(size: 10.8pt, weight: 600, style: "italic"); it.body })
  show heading.where(level: 5): it => block(above: 10pt, below: 8pt, sticky: true, {
    set par(justify: false); set text(font: SERIF, size: 10.5pt, weight: 700); it.body })
  body
}

#let chapter-no(n) = block(width: 100%, below: 7.6pt, align(right, text(font: SANS, size: 33pt, weight: 700, fill: K(75%), n)))

#let list-item(level: 1, marker: "•", body) = {
  let lead = if level == 1 { 27pt } else { 38pt }
  let at = if level == 1 { 14pt } else { 26pt }
  block(above: 8pt, below: 8pt, width: 100%, inset: (left: lead), {
    place(dx: at - lead, marker)
    body
  })
}

#let note(title, body) = block(width: 100%, stroke: .5pt + band, radius: 5pt, inset: (x: 14pt, top: 8pt, bottom: 10pt), above: 12pt, below: 13pt, breakable: true, {
  set par(leading: 2pt, spacing: 4.9pt)
  if title != none { block(sticky: true, below: 4.9pt, text(weight: 600, title)) }
  body
})

#let codeblock(lines, bold: false) = block(width: 100%, fill: rgb(95.3%, 94.9%, 94.5%), inset: (left: 9pt, right: 3pt, y: 4.5pt), above: 7pt, below: 11pt, breakable: true, {
  set par(justify: false, leading: 3.33pt, spacing: 3.33pt)
  set text(font: MONO, size: 9pt, fill: codeink, hyphenate: false, weight: if bold { 700 } else { 400 })
  show raw: set text(font: MONO, size: 9pt, fill: codeink, weight: if bold { 700 } else { 400 })
  stack(dir: ttb, spacing: 3.33pt, ..lines.map(l => box(height: 9pt, width: 100%, if l == "" { [] } else { raw(l) })))
})

#let caption(body, sticky: false) = block(width: 100%, above: 13pt, below: 16pt, sticky: sticky, {
  set align(center); set par(justify: false)
  text(font: SANS, size: 9pt, hyphenate: false, body) })

// keep: the text of a figure is not split, and its caption stays with it (sticky).
#let figure-text(label, body, keep: false) = block(width: 100%, above: 6pt, below: 14pt, inset: (x: 18pt), breakable: not keep, {
  set par(justify: false, leading: 2.4pt)
  set text(font: SANS, size: 8.5pt, hyphenate: false)
  text(weight: 600, label + ": ")
  body
})

// sticky: a figure stays on the page of its caption.
#let figure-block(path, width, alt: none) = block(above: 10pt, below: 0pt, breakable: false, sticky: true, width: 100%, align(center, image(path, width: width, alt: alt)))

// A cover fits whole on its own page: the rebuilt page is smaller than the source image.
#let full-page-image(path, mark, alt: none) = page(margin: 0pt, header: none, footer: none, { mark; align(center + horizon, image(path, width: 100%, height: 100%, fit: "contain", alt: alt)) })

#let table-block(columns, cells) = block(above: 10pt, below: 12pt, {
  table(
    columns: columns.map(c => c * 1fr),
    stroke: .5pt + ink,
    inset: (x: 5pt, y: 4pt),
    align: left + top,
    ..cells
  )
})

#let toc-title(t) = block(below: 40pt, align(right, text(font: SANS, size: 38pt, weight: 700, tracking: .04em, t)))
#let toc = context {
  for h in query(heading.where(outlined: true)) {
    let loc = h.location()
    let n = pnum(loc)
    let row(sz, f, w, above, col: ink, stroke: none) = block(above: above, below: 0pt, stroke: stroke, inset: (bottom: if stroke == none { 0pt } else { 3pt }), link(loc, grid(columns: (1fr, auto), column-gutter: 12pt, { set par(justify: false); text(font: f, size: sz, weight: w, fill: col, hyphenate: false, h.body) }, text(font: f, size: sz, weight: w, fill: col, n))))
    if h.level == 1 { row(13pt, SANS, 600, 20pt, stroke: (bottom: .5pt + ink)) }
    else if h.level == 2 { row(11pt, SERIF, 600, 8pt) }
    else { row(9pt, SERIF, 600, 5pt, col: K(80%)) }
  }
}

#let band-page(side, body) = page(background: place(top + side, rect(width: 18.18pt, height: 100%, fill: band)), foreground: none, body)

#let part-page(title, body) = {
  pagebreak(to: "odd", weak: true)
  band-page(right, {
    v(55.6pt)
    block(width: 100%, below: 40pt, {
      set align(right); set par(justify: false, leading: 10pt)
      set text(font: SANS, size: 25pt, weight: 700, fill: K(75%), tracking: .06em, hyphenate: false)
      show heading: it => it.body  // listed in the contents, drawn by this block in the part colour
      title
    })
    set text(fill: K(75%))
    body
  })
  band-page(left, [])
}

#let index-letter(l) = block(above: 14pt, below: 7pt, text(font: SANS, size: 18pt, weight: 700, l))
#let index-entry(term, refs, main: true) = block(above: 4pt, below: 0pt, inset: (left: if main { 0pt } else { 7.5pt }), {
  set par(justify: false, hanging-indent: 8pt)
  set text(hyphenate: false)
  if main { text(weight: 700, term) } else { term }
  h(.5em)
  refs
})

// Unit positions for the artifact checks: `typst query <file> "<positions>" --field value --one`.
#let positions = context [#metadata(query(<unit>).map(m => (id: m.value, page: m.location().page(), y: m.location().position().y.pt())))<positions>]
