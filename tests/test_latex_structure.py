"""Test della struttura LaTeX: sezioni numerate, inglobazione e template."""

from __future__ import annotations

import unittest

from study_manager.services.pdf_tools.document import RenderOptions
from study_manager.services.pdf_tools.export import (
    CONTENT_MARKER,
    ExportError,
    apply_template,
    latex_to_markdown,
    markdown_to_latex,
    SectionNumbering,
    heading_level_name,
    numbering_for,
    render_source,
)

MD = "=== SLIDE 1 ===\n\n# Titolo\n\nTesto $x^2$\n\n## Sotto\n\nAltro\n"

TEMPLATE = r"""\documentclass{article}
\usepackage{amsmath}
\usepackage[hidelinks]{hyperref}
\title{}
\begin{document}
{\Huge {{TITOLO}}}
\tableofcontents
\end{document}
"""


class LatexStructureTests(unittest.TestCase):
    def test_numbered_and_unnumbered(self) -> None:
        numbered = markdown_to_latex(MD, RenderOptions(include_preamble=False))
        self.assertIn("\\section{Titolo}", numbered)
        self.assertIn("\\subsection{Sotto}", numbered)
        plain = markdown_to_latex(MD, RenderOptions(include_preamble=False, numbered_sections=False))
        self.assertIn("\\section*{Titolo}", plain)
        self.assertIn("\\subsection*{Sotto}", plain)

    def test_embed_shifts_levels_and_adds_root(self) -> None:
        options = RenderOptions(embed=True, base_level="section", root_title="Reti & protocolli")
        latex = markdown_to_latex(MD, options)
        self.assertTrue(latex.startswith("\\section{Reti \\& protocolli}"))
        self.assertIn("\\subsection{Titolo}", latex)
        self.assertIn("\\subsubsection{Sotto}", latex)
        self.assertNotIn("\\documentclass", latex)  # frammento, anche col preambolo attivo

    def test_embed_from_chapter(self) -> None:
        options = RenderOptions(embed=True, base_level="chapter", root_title="Capitolo")
        latex = markdown_to_latex(MD, options)
        self.assertTrue(latex.startswith("\\chapter{Capitolo}"))
        self.assertIn("\\section{Titolo}", latex)

    def test_latex_roundtrip_with_embed(self) -> None:
        options = RenderOptions(embed=True, base_level="subsection", root_title="X")
        editor = markdown_to_latex(MD, options, full=False, root=False)
        self.assertNotIn("{X}", editor)
        self.assertIn("\\subsubsection{Titolo}", editor)
        back = latex_to_markdown(editor, options)
        self.assertIn("# Titolo", back)
        self.assertIn("## Sotto", back)
        exported = render_source(editor, "latex", "tex", options)
        self.assertTrue(exported.startswith("\\subsection{X}"))

    def test_template_before_end_document(self) -> None:
        options = RenderOptions(title="Lezione 1", template=TEMPLATE)
        latex = markdown_to_latex(MD, options)
        self.assertIn("\\title{Lezione 1}", latex)
        self.assertIn("{\\Huge Lezione 1}", latex)
        self.assertIn("\\usepackage{amssymb} % aggiunto da StudyFlow", latex)
        self.assertEqual(latex.count("\\usepackage{amsmath}"), 1)
        self.assertLess(latex.index("\\tableofcontents"), latex.index("\\section{Titolo}"))
        self.assertLess(latex.index("\\section{Titolo}"), latex.index("\\end{document}"))
        self.assertEqual(latex.count("\\documentclass"), 1)

    def test_template_marker(self) -> None:
        template = TEMPLATE.replace("\\tableofcontents", f"\\tableofcontents\n{CONTENT_MARKER}\n\\appendix")
        latex = apply_template(template, "\\section{A}\n", RenderOptions())
        self.assertNotIn(CONTENT_MARKER, latex)
        self.assertLess(latex.index("\\section{A}"), latex.index("\\appendix"))

    def test_template_ignored_when_embedding(self) -> None:
        options = RenderOptions(embed=True, root_title="R", template=TEMPLATE)
        self.assertNotIn("\\documentclass", markdown_to_latex(MD, options))

    def test_invalid_template(self) -> None:
        with self.assertRaises(ExportError):
            apply_template("\\section{solo}", "x", RenderOptions())


class NumberingTests(unittest.TestCase):
    def test_article_numbering(self) -> None:
        n = SectionNumbering()
        steps = ["section", "subsection", "subsection", "subsubsection", "section", "subsection", "paragraph"]
        self.assertEqual([n.step(x) for x in steps], ["1", "1.1", "1.2", "1.2.1", "2", "2.1", ""])

    def test_chapter_and_part(self) -> None:
        n = SectionNumbering()
        steps = ["chapter", "section", "part", "section", "chapter", "section"]
        self.assertEqual([n.step(x) for x in steps], ["Capitolo 1", "1.1", "Parte I", "1.2", "Capitolo 2", "2.1"])

    def test_starred_not_counted(self) -> None:
        n = SectionNumbering()
        self.assertEqual(n.step("section", starred=True), "")
        self.assertEqual(n.step("section"), "1")

    def test_starts_from_one_without_template_content(self) -> None:
        self.assertEqual(numbering_for(RenderOptions()).step("section"), "1")
        marker = TEMPLATE.replace("\\tableofcontents", "\\tableofcontents\n% STUDYFLOW:CONTENUTO")
        self.assertEqual(numbering_for(RenderOptions(template=marker)).step("section"), "1")

    def test_continues_after_template_sections(self) -> None:
        template = TEMPLATE.replace(
            "\\tableofcontents", "\\tableofcontents\n\\section{Prima}\n% \\section{commentata}\n\\section*{Senza}"
        )
        self.assertEqual(numbering_for(RenderOptions(template=template)).step("section"), "2")
        # in inglobazione il documento ospite non e' noto: si parte da 1
        embed = RenderOptions(template=template, embed=True)
        self.assertEqual(numbering_for(embed).step("subsection"), "0.1")

    def test_embed_levels(self) -> None:
        options = RenderOptions(embed=True, base_level="section")
        self.assertEqual(heading_level_name(1, options), "subsection")
        n = SectionNumbering()
        self.assertEqual([n.step("section"), n.step(heading_level_name(1, options))], ["1", "1.1"])


if __name__ == "__main__":
    unittest.main()
