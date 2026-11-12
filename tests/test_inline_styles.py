"""Test della formattazione in linea: sottolineato, monospazio, colori."""

from __future__ import annotations

import unittest

from study_manager.services.pdf_tools.document import RenderOptions
from study_manager.services.pdf_tools.export import (
    apply_template,
    latex_to_markdown,
    markdown_to_latex,
    markdown_to_txt,
    slides_to_pandoc_markdown,
    source_to_html,
)
from study_manager.services.pdf_tools.document import parse_markdown
from study_manager.services.pdf_tools.inline import (
    STYLE_COLOR,
    STYLE_MONO,
    STYLE_UNDERLINE,
    Math,
    Styled,
    iter_runs,
    parse_inline,
    preview_color,
    strip_inline_styles,
)

LINE = r"Vedi \underline{il teorema $T_1$} e \texttt{int x = 0;} oppure \textcolor{red}{\textbf{attenzione}} con 30\$"
MD = "=== SLIDE 1 ===\n\n# Titolo\n\n" + LINE + "\n\n- \\underline{punto & virgola}\n"


class ParseTests(unittest.TestCase):
    def test_tree(self) -> None:
        nodes = parse_inline(LINE)
        underline = nodes[1]
        self.assertIsInstance(underline, Styled)
        self.assertEqual(underline.style, STYLE_UNDERLINE)
        self.assertIsInstance(underline.children[1], Math)
        self.assertEqual(nodes[3].style, STYLE_MONO)
        color = nodes[5]
        self.assertEqual((color.style, color.value), (STYLE_COLOR, "red"))
        runs = list(iter_runs(nodes))
        self.assertIn(("text", "attenzione", (("color", "red"), ("bold", ""))), runs)

    def test_unclosed_and_math_untouched(self) -> None:
        self.assertEqual(parse_inline(r"\underline{aperto"), [r"\underline{aperto"])
        nodes = parse_inline(r"$\underline{x}$ e \texttt{a\}b}")
        self.assertIsInstance(nodes[0], Math)
        self.assertEqual(nodes[2].children, [r"a\}b"])

    def test_html_color_model(self) -> None:
        node = parse_inline(r"\textcolor[HTML]{FF0000}{x}")[0]
        self.assertEqual((node.model, node.value), ("HTML", "FF0000"))
        self.assertEqual(preview_color("FF0000"), "#FF0000")
        self.assertEqual(preview_color("blue!50"), preview_color("blue"))
        self.assertIsNone(preview_color("inventato"))

    def test_strip(self) -> None:
        self.assertEqual(strip_inline_styles(r"**a** \underline{b} \textcolor{red}{c}"), "a b c")


class ExportStyleTests(unittest.TestCase):
    def test_latex(self) -> None:
        latex = markdown_to_latex(MD, RenderOptions(include_preamble=False))
        self.assertIn(r"\underline{il teorema $T_1$}", latex)
        self.assertIn(r"\texttt{int x = 0;}", latex)
        self.assertIn(r"\textcolor{red}{\textbf{attenzione}}", latex)
        self.assertIn(r"\item \underline{punto \& virgola}", latex)
        self.assertIn(r"30\$", latex)
        full = markdown_to_latex(MD, RenderOptions(include_preamble=True))
        self.assertIn(r"\usepackage{xcolor}", full)

    def test_latex_roundtrip_keeps_commands(self) -> None:
        latex = markdown_to_latex(MD, RenderOptions(include_preamble=False))
        back = latex_to_markdown(latex)
        self.assertIn(r"\underline{il teorema $T_1$}", back)
        self.assertIn(r"\texttt{int x = 0;}", back)
        self.assertIn(r"\textcolor{red}{**attenzione**}", back)

    def test_txt(self) -> None:
        text = markdown_to_txt(MD, RenderOptions(include_markers=False))
        self.assertIn("Vedi il teorema T₁ e int x = 0; oppure attenzione con 30$", text)

    def test_pandoc(self) -> None:
        source = slides_to_pandoc_markdown(parse_markdown(MD), RenderOptions())
        self.assertIn("[il teorema $T_1$]{.underline}", source)
        self.assertIn("`int x = 0;`", source)
        self.assertIn("**attenzione**", source)
        self.assertNotIn("textcolor", source)

    def test_html(self) -> None:
        page = source_to_html(MD, "markdown", RenderOptions())
        self.assertIn("<u>il teorema $T_1$</u>", page)
        self.assertIn("<code>int x = 0;</code>", page)
        self.assertIn('<span style="color:', page)

    def test_template_adds_xcolor_only_when_needed(self) -> None:
        template = "\\documentclass{article}\n\\usepackage{amsmath,amssymb}\n\\begin{document}\n\\end{document}\n"
        self.assertIn("\\usepackage{xcolor}", apply_template(template, r"\textcolor{red}{x}", RenderOptions()))
        self.assertNotIn("xcolor", apply_template(template, r"\underline{x}", RenderOptions()))
        with_box = template.replace("\\begin{document}", "\\usepackage[most]{tcolorbox}\n\\begin{document}")
        self.assertNotIn("{xcolor}", apply_template(with_box, r"\textcolor{red}{x}", RenderOptions()))


if __name__ == "__main__":
    unittest.main()
