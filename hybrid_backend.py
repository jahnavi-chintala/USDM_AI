"""Hybrid PDF backend: docling-parse for text, pdfium for page images.

docling-parse extracts cleaner text than pypdfium2 (no spaces injected around
hyphens, intact dot leaders), but its renderer draws some embedded images in
this protocol as blank boxes, so the layout model never sees figures such as
the study schema. This backend keeps every docling-parse feature (text cells,
shapes, bitmap rects for OCR) and swaps only the page image for a pdfium render.
"""

from io import BytesIO
from pathlib import Path
from typing import Iterator, Optional, Union

import pypdfium2 as pdfium
from docling.backend.docling_parse_backend import (
    ThreadedDoclingParseDocumentBackend,
    ThreadedDoclingParsePageBackend,
)
from docling.backend.pdf_backend import PdfBackendOptions
from docling.datamodel.document import InputDocument
from docling.utils.locks import pypdfium2_lock
from docling_core.types.doc import BoundingBox, CoordOrigin
from PIL import Image


class HybridPageBackend(ThreadedDoclingParsePageBackend):
    def __init__(self, base: ThreadedDoclingParsePageBackend, pdoc: pdfium.PdfDocument):
        super().__init__(base._result, rendered=base._rendered)
        self._pdoc = pdoc

    def get_page_image(
        self, scale: float = 1, cropbox: Optional[BoundingBox] = None
    ) -> Image.Image:
        # Same rendering as docling's PyPdfiumPageBackend.get_page_image.
        size = self.get_size()
        if not cropbox:
            cropbox = BoundingBox(l=0, r=size.width, t=0, b=size.height,
                                  coord_origin=CoordOrigin.TOPLEFT)
            padbox = BoundingBox(l=0, r=0, t=0, b=0, coord_origin=CoordOrigin.BOTTOMLEFT)
        else:
            padbox = cropbox.to_bottom_left_origin(size.height).model_copy()
            padbox.r = size.width - padbox.r
            padbox.t = size.height - padbox.t

        with pypdfium2_lock:
            page = self._pdoc[self.page_no - 1]
            try:
                bitmap = page.render(scale=scale * 1.5, rotation=0, crop=padbox.as_tuple())
                image = bitmap.to_pil().copy()
                bitmap.close()
            finally:
                page.close()
        # Render at 1.5x and downsample for a sharper image.
        return image.resize(
            (round(cropbox.width * scale), round(cropbox.height * scale))
        ).convert("RGB")


class HybridPdfBackend(ThreadedDoclingParseDocumentBackend):
    def __init__(
        self,
        in_doc: InputDocument,
        path_or_stream: Union[BytesIO, Path],
        options: Optional[PdfBackendOptions] = None,
    ):
        super().__init__(in_doc, path_or_stream, options)
        source = self.path_or_stream
        if isinstance(source, BytesIO):
            source = BytesIO(source.getvalue())
        password = getattr(options, "password", None)  # the same password docling-parse gets
        with pypdfium2_lock:
            self._pdoc = pdfium.PdfDocument(source, password=password)

    def iter_pages(self) -> Iterator[HybridPageBackend]:
        for page in super().iter_pages():
            yield HybridPageBackend(page, self._pdoc)

    def unload(self) -> None:
        super().unload()
        with pypdfium2_lock:
            if self._pdoc is not None:
                self._pdoc.close()
                self._pdoc = None
