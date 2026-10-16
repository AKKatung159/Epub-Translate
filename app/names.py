"""Readable book names from download file names: "_OceanofPDF.com_The_Goblin_Emperor_-_Katherine_Addison.epub"
-> "The Goblin Emperor - Katherine Addison". Used for jobs, PDF conversions and the files sent to the X4."""
from __future__ import annotations

import re

# download-site tags that end up in file names
SITE_TAGS = re.compile(
    r"(?i)(?:^|[\s_\-\[(])(?:OceanofPDF\.com|oceanofpdf|z-?lib(?:rary)?(?:\.org)?|libgen(?:\.\w+)?|"
    r"anna[’']?s[ _-]archive|pdfdrive(?:\.com)?|ebook3000|dokumen\.pub|epub\.pub|www\.\w+\.\w+)"
    r"(?:[\s_\-\])]|$)")
EXT = re.compile(r"(?i)\.(epub|pdf|xtch?|azw3|mobi)$")


def clean_name(raw: str) -> str:
    name = EXT.sub("", (raw or "").strip())
    for _ in range(3):                                      # tags can be stacked: "(z-lib.org)_OceanofPDF.com_..."
        name = SITE_TAGS.sub(" ", name)
    name = re.sub(r"_-_|\s*_\s*-\s*_?\s*", " - ", name)     # "Title_-_Author" -> "Title - Author"
    name = name.replace("_", " ")
    name = re.sub(r"\s*-\s*$|^\s*-\s*", "", name)
    name = re.sub(r"\(\s*\)|\[\s*\]", "", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" .-")
    return name or (raw or "book").strip()
