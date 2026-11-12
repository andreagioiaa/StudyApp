"""Test del modulo PDF e Slide: parsing, formule, pulizia ed esportazione."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from study_manager.services.pdf_tools.controller import PdfConversionController, list_pdfs
from study_manager.services.pdf_tools.document import (
    BLOCK_BULLET,
    BLOCK_HEADING,
    BLOCK_MATH,
    BLOCK_NUMBER,
    BLOCK_PARAGRAPH,
    Block,
    PdfDocument,
    RenderOptions,
    Slide,
    join_lines,
    parse_markdown,
    render_markdown,
    split_inline_math,
)
from study_manager.services.pdf_tools.export import (
    FORMAT_TEX,
    FORMAT_TXT,
    SOURCE_LATEX,
    export_file,
    find_pandoc,
    latex_escape,
    latex_to_markdown,
    markdown_to_latex,
    markdown_to_txt,
    slides_to_pandoc_markdown,
    source_to_html,
)
from study_manager.services.pdf_tools.extract import (
    ExtractOptions,
    build_engine_args,
    convert_pdf,
    import_fitz,
    parse_tesseract_tsv,
)
from study_manager.services.pdf_tools.layout import Line, PageLines, analyze_page, strip_repeated_margins
from study_manager.services.pdf_tools.mathconv import (
    latex_to_unicode,
    normalize_math_delimiters,
    unicode_math_to_latex,
    wrap_unicode_math,
)
from study_manager.services.pdf_tools.mathlayout import Atom, atoms_to_latex


SAMPLE = """=== SLIDE 1 ===

# Titolo & prova

Testo con $x^{2}$ e costo 30\\$.

- primo
  - annidato
1. uno
2. due

$$
\\frac{a}{b}
$$

=== SLIDE 2 ===

## Seconda
Paragrafo su
due righe.
"""


class DocumentFormatTests(unittest.TestCase):
    def test_parse_slides_and_blocks(self) -> None:
        slides = parse_markdown(SAMPLE)
        self.assertEqual([slide.number for slide in slides], [1, 2])
        kinds = [block.kind for block in slides[0].blocks]
        self.assertEqual(
            kinds,
            [BLOCK_HEADING, BLOCK_PARAGRAPH, BLOCK_BULLET, BLOCK_BULLET, BLOCK_NUMBER, BLOCK_NUMBER, BLOCK_MATH],
        )
        self.assertEqual(slides[0].blocks[3].level, 1)
        self.assertEqual(slides[0].blocks[5].number, 2)
        self.assertEqual(slides[0].blocks[6].text, "\\frac{a}{b}")
        self.assertEqual(slides[1].blocks[1].text, "Paragrafo su due righe.")

    def test_text_without_markers_is_one_slide(self) -> None:
        slides = parse_markdown("Solo testo\n\n- punto")
        self.assertEqual(len(slides), 1)
        self.assertEqual(slides[0].number, 1)

    def test_render_roundtrip(self) -> None:
        document = PdfDocument(
            source=Path("x.pdf"),
            title="x",
            slides=[
                Slide(1, [Block(BLOCK_HEADING, "Titolo", 1), Block(BLOCK_BULLET, "a", 0), Block(BLOCK_BULLET, "b", 1)]),
                Slide(2, [Block(BLOCK_MATH, "E = mc^{2}")]),
            ],
        )
        markdown = render_markdown(document)
        self.assertIn("=== SLIDE 1 ===", markdown)
        self.assertIn("  - b", markdown)
        reparsed = parse_markdown(markdown)
        self.assertEqual(reparsed[1].blocks[0].text, "E = mc^{2}")

    def test_split_inline_math(self) -> None:
        self.assertEqual(
            split_inline_math("a $x$ b \\$ c \\(y\\)"),
            [(False, "a "), (True, "x"), (False, " b \\$ c "), (True, "y")],
        )
        self.assertEqual(split_inline_math("costo $5"), [(False, "costo $5")])

    def test_dehyphenation(self) -> None:
        self.assertEqual(join_lines(["infor-", "mazione utile"]), "informazione utile")
        self.assertEqual(join_lines(["Rossi-", "Bianchi"]), "Rossi- Bianchi")
        self.assertEqual(join_lines(["parola", "successiva"]), "parola successiva")


class MathConversionTests(unittest.TestCase):
    def test_unicode_to_latex(self) -> None:
        self.assertEqual(unicode_math_to_latex("x² ≤ α"), "x^{2} \\leq \\alpha")
        self.assertEqual(unicode_math_to_latex("√(a+b)"), "\\sqrt{a+b}")
        self.assertEqual(unicode_math_to_latex("x₁₀"), "x_{10}")

    def test_wrap_formulas_in_text(self) -> None:
        self.assertEqual(wrap_unicode_math("Energia: E = ½ m v²"), "Energia: $E = \\frac{1}{2} m v^{2}$")
        self.assertEqual(wrap_unicode_math("se x ≥ 0, allora"), "se $x \\geq 0$, allora")
        # Le frecce nella prosa non diventano formule.
        self.assertEqual(wrap_unicode_math("Studio ⇒ risultati"), "Studio ⇒ risultati")
        self.assertEqual(wrap_unicode_math("costa 5$"), "costa 5\\$")

    def test_latex_to_unicode(self) -> None:
        self.assertEqual(latex_to_unicode("x^{2} + y_{1}"), "x² + y₁")
        self.assertEqual(latex_to_unicode("\\frac{1}{1-q}"), "1/(1-q)")
        self.assertEqual(latex_to_unicode("\\sqrt{x^{2} + y^{2}}"), "√(x² + y²)")
        self.assertEqual(latex_to_unicode("\\alpha \\in \\mathbb{R}"), "α ∈ ℝ")

    def test_nougat_delimiters(self) -> None:
        self.assertEqual(normalize_math_delimiters("a \\(x\\) b"), "a $x$ b")
        self.assertIn("$$\nx^2\n$$", normalize_math_delimiters("\\[x^2\\]"))

    def test_geometric_fraction_and_limits(self) -> None:
        def atom(text, x0, x1, y, size=10.0, **kwargs):
            return Atom(text=text, x0=x0, x1=x1, y0=y - size, y1=y + 2, origin_y=y, size=size, **kwargs)

        atoms = [
            atom("Z", 0, 6, 100, font="CMEX10"),
            atom("0", 6, 9, 106, size=7),
            atom("1", 7, 10, 90, size=7),
            atom("x", 12, 17, 100, font="CMMI10"),
            atom("=", 20, 26, 100),
            atom("1", 29, 34, 93),
            atom("3", 29, 34, 108),
        ]
        # Il simbolo CMEX "Z" e' l'integrale in stile display.
        from study_manager.services.pdf_tools.mathlayout import atom_from_span

        atoms[0] = atom_from_span({"text": "Z", "bbox": (0, 88, 6, 108), "origin": (0, 96), "size": 10, "font": "CMEX10"})
        self.assertEqual(atoms_to_latex(atoms), "\\int_{0}^{1}x = \\frac{1}{3}")


class LayoutTests(unittest.TestCase):
    def _page(self, number: int, lines: list[Line]) -> PageLines:
        return PageLines(number=number, width=960, height=540, lines=lines)

    def test_title_bullets_and_numbering(self) -> None:
        lines = [
            Line("Titolo slide", 60, 50, 400, 84, size=30),
            Line("• Primo punto", 60, 150, 400, 170, size=18),
            Line("• Secondo punto che va", 60, 190, 500, 210, size=18),
            Line("a capo", 80, 212, 200, 232, size=18),
            Line("– sotto-punto", 100, 240, 400, 260, size=18),
            Line("1. numerato", 60, 280, 400, 300, size=18),
        ]
        blocks = analyze_page(self._page(1, lines))
        self.assertEqual(blocks[0].kind, BLOCK_HEADING)
        self.assertEqual(blocks[0].text, "Titolo slide")
        self.assertEqual(blocks[2].text, "Secondo punto che va a capo")
        self.assertEqual((blocks[3].kind, blocks[3].level), (BLOCK_BULLET, 1))
        self.assertEqual((blocks[4].kind, blocks[4].number), (BLOCK_NUMBER, 1))

    def test_repeated_headers_and_page_numbers(self) -> None:
        pages = []
        for number in range(1, 5):
            pages.append(
                self._page(
                    number,
                    [
                        Line("Corso di Analisi - Lezione 3", 60, 10, 400, 22, size=9),
                        Line(f"Contenuto {number}", 60, 200, 400, 220, size=18),
                        Line(str(number), 900, 515, 910, 527, size=9),
                    ],
                )
            )
        removed = strip_repeated_margins(pages)
        self.assertEqual(removed, 8)
        self.assertEqual([line.text for line in pages[2].lines], ["Contenuto 3"])


class ExportTests(unittest.TestCase):
    def test_latex_body_and_full_document(self) -> None:
        options = RenderOptions(include_preamble=False)
        body = markdown_to_latex(SAMPLE, options)
        self.assertIn("% === Slide 1 ===", body)
        self.assertIn("\\section{Titolo \\& prova}", body)
        self.assertIn("costo 30\\$", body)
        self.assertIn("\\begin{itemize}\n  \\item primo\n  \\begin{itemize}\n    \\item annidato", body)
        self.assertIn("\\begin{enumerate}", body)
        self.assertIn("\\[\n\\frac{a}{b}\n\\]", body)
        self.assertNotIn("\\documentclass", body)
        full = markdown_to_latex(SAMPLE, RenderOptions(include_preamble=True, title="Prova"))
        self.assertTrue(full.startswith("\\documentclass"))
        self.assertIn("\\usepackage{amsmath}", full)
        self.assertIn("\\end{document}", full)

    def test_toggles_markers_and_headings(self) -> None:
        text = markdown_to_txt(SAMPLE, RenderOptions(include_markers=False, include_headings=False))
        self.assertNotIn("=== SLIDE", text)
        self.assertNotIn("Titolo", text)
        text = markdown_to_txt(SAMPLE, RenderOptions())
        self.assertIn("=== SLIDE 2 ===", text)
        self.assertIn("Testo con x² e costo 30$.", text)
        self.assertIn("  - annidato", text)

    def test_latex_escape_symbols(self) -> None:
        self.assertEqual(latex_escape("50% & a_b → c"), "50\\% \\& a\\_b \\ensuremath{\\to} c")

    def test_latex_roundtrip(self) -> None:
        latex = markdown_to_latex(SAMPLE, RenderOptions(include_preamble=False))
        markdown = latex_to_markdown(latex)
        slides = parse_markdown(markdown)
        self.assertEqual(len(slides), 2)
        self.assertEqual(slides[0].blocks[0].text, "Titolo & prova")
        self.assertIn("$x^{2}$", slides[0].blocks[1].text)
        self.assertEqual(slides[0].blocks[3].level, 1)

    def test_pandoc_markdown_math(self) -> None:
        source = slides_to_pandoc_markdown(parse_markdown(SAMPLE), RenderOptions())
        self.assertIn('::: {custom-style="Slide Marker"}', source)
        self.assertIn("$x^{2}$", source)
        self.assertIn("$$\\frac{a}{b}$$", source)
        self.assertIn("30\\$", source)

    def test_export_text_files(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            tex = export_file(SAMPLE, "markdown", FORMAT_TEX, Path(folder) / "a.tex", RenderOptions())
            txt = export_file(SAMPLE, "markdown", FORMAT_TXT, Path(folder) / "a.txt", RenderOptions())
            self.assertIn("\\begin{document}", tex.read_text(encoding="utf-8"))
            self.assertIn("=== SLIDE 1 ===", txt.read_text(encoding="utf-8"))
            latex_source = "\\section*{Prova}\n\nTesto $a$."
            out = export_file(latex_source, SOURCE_LATEX, FORMAT_TEX, Path(folder) / "b.tex",
                              RenderOptions(include_preamble=False))
            self.assertEqual(out.read_text(encoding="utf-8").strip(), latex_source)

    @unittest.skipUnless(find_pandoc(), "Pandoc non disponibile")
    def test_docx_has_native_equations(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = export_file(SAMPLE, "markdown", "docx", Path(folder) / "a.docx", RenderOptions(title="Prova"))
            with zipfile.ZipFile(target) as archive:
                xml = archive.read("word/document.xml").decode("utf-8")
            self.assertIn("<m:oMath", xml)
            self.assertIn('w:val="Heading1"', xml)

    def test_html_preview_uses_local_katex(self) -> None:
        page = source_to_html(SAMPLE, "markdown", RenderOptions(title="Prova"))
        self.assertIn("katex.min.js", page)
        self.assertIn("file:", page)
        self.assertNotIn("cdn", page.lower())
        self.assertIn('<span class="dollar">$</span>', page)


class EngineHelpersTests(unittest.TestCase):
    def test_tesseract_tsv(self) -> None:
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t100\t100\t80\t40\t95\tTitolo\n"
            "5\t1\t1\t1\t1\t2\t190\t100\t60\t40\t95\tgrande\n"
            "5\t1\t2\t1\t1\t1\t100\t300\t50\t20\t90\tTesto\n"
        )
        lines = parse_tesseract_tsv(tsv, scale=0.5)
        self.assertEqual([line.text for line in lines], ["Titolo grande", "Testo"])
        self.assertAlmostEqual(lines[0].x0, 50.0)
        self.assertGreater(lines[0].size, lines[1].size)

    def test_engine_command_placeholders(self) -> None:
        inputs = [Path("/tmp/in/p0001.pdf"), Path("/tmp/in/p0002.pdf")]
        args = build_engine_args("nougat {inputs} -o {output_dir}", Path("/tmp/in"), Path("/tmp/out"), inputs)
        self.assertEqual(args[1:], [str(inputs[0]), str(inputs[1]), "-o", str(Path("/tmp/out"))])
        args = build_engine_args("marker {input_dir} --output_dir {output_dir}", Path("/tmp/in"), Path("/tmp/out"), inputs)
        self.assertEqual(args[1:], [str(Path("/tmp/in")), "--output_dir", str(Path("/tmp/out"))])


@unittest.skipUnless(import_fitz() is not None, "PyMuPDF non installato")
class PdfExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.pdf = Path(self.folder.name) / "lezione.pdf"
        fitz = import_fitz()
        document = fitz.open()
        for number in range(1, 4):
            page = document.new_page(width=960, height=540)
            page.insert_text((60, 90), f"Argomento {number}", fontsize=32)
            page.insert_text((60, 160), "\u2022 Primo concetto importante", fontsize=20)
            page.insert_text((100, 200), "\u2013 dettaglio del concetto", fontsize=20)
            page.insert_text((60, 240), "\u2022 Secondo concetto", fontsize=20)
            page.insert_text((60, 300), "Questo paragrafo contiene una parola divi-", fontsize=20)
            page.insert_text((60, 325), "sa su due righe.", fontsize=20)
            page.insert_text((60, 525), "Corso di Ingegneria del Software", fontsize=9)
            page.insert_text((900, 525), str(number), fontsize=9)
        document.save(str(self.pdf))
        document.close()

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_convert_slides(self) -> None:
        result = convert_pdf(self.pdf, ExtractOptions(ocr_mode="off"))
        self.assertEqual(result.page_count, 3)
        markdown = render_markdown(result)
        self.assertIn("=== SLIDE 2 ===", markdown)
        self.assertIn("# Argomento 2", markdown)
        self.assertIn("- Primo concetto importante", markdown)
        self.assertIn("  - dettaglio del concetto", markdown)
        self.assertIn("parola divisa su due righe.", markdown)
        self.assertNotIn("Corso di Ingegneria", markdown)
        self.assertNotIn("\n3\n", markdown)

    def test_cancel(self) -> None:
        cancel = threading.Event()
        cancel.set()
        from study_manager.services.pdf_tools.extract import PdfCancelled

        with self.assertRaises(PdfCancelled):
            convert_pdf(self.pdf, ExtractOptions(ocr_mode="off"), cancel=cancel)

    def test_controller_batch(self) -> None:
        second = Path(self.folder.name) / "altro.pdf"
        second.write_bytes(self.pdf.read_bytes())
        broken = Path(self.folder.name) / "rotto.pdf"
        broken.write_bytes(b"non e' un pdf")
        sources = list_pdfs(self.folder.name)
        self.assertEqual([path.name for path in sources], ["altro.pdf", "lezione.pdf", "rotto.pdf"])
        controller = PdfConversionController()
        ok, _ = controller.start(sources, ExtractOptions(ocr_mode="off"))
        self.assertTrue(ok)
        events = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                event = controller.events.get(timeout=0.2)
            except Exception:
                continue
            events.append(event)
            if event["type"] == "done":
                break
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual((done["converted"], done["failed"]), (2, 1))
        self.assertTrue(any(event["type"] == "progress" for event in events))


if __name__ == "__main__":
    unittest.main()
