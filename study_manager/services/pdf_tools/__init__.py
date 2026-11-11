"""Trascrizione e conversione locale di PDF e slide in TXT, LaTeX e Word.

Il pacchetto e' diviso in moduli senza dipendenze dalla GUI:

- ``document``: modello a blocchi e formato sorgente Markdown con marcatori
  ``=== SLIDE N ===`` (il testo che l'utente modifica nell'editor);
- ``mathconv``: conversione Unicode <-> LaTeX per le formule;
- ``cleanup``: rimozione di intestazioni, numeri di pagina e sillabazioni;
- ``extract``: motori di estrazione (PyMuPDF, pdfplumber, OCR, Marker, Nougat);
- ``text_removal``: rimozione del testo ridondante ("Rimuovi testo") e regole salvate;
- ``export``: generazione di .txt, .tex, .docx (Pandoc) e anteprima HTML KaTeX;
- ``controller``: esecuzione in background, batch, avanzamento e annullamento.

Tutto gira in locale: nessun modulo effettua chiamate di rete.
"""

from .document import Block, PdfDocument, RenderOptions, Slide, parse_markdown, render_markdown

__all__ = [
    "Block",
    "PdfDocument",
    "RenderOptions",
    "Slide",
    "parse_markdown",
    "render_markdown",
]
