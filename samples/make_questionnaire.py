"""Make the sample questionnaire a fictional bank sends: samples/questionnaire.pdf (with a text layer) and
samples/questionnaire-page1.png (a screenshot of page 1, no text layer), plus the ground truth in
samples/questionnaire-truth.json. Development only: needs reportlab, pypdfium2 and Pillow.

    python samples/make_questionnaire.py

Questions marked VSAQ are Google's (Apache License 2.0, see third_party/vsaq); the rest are this project's.
"""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent.parent
BANK = "Northwind Bank (fictional)"
SECTIONS = [
    ("A. Access control", [
        ("A1", "Is multi-factor authentication enforced for all administrative and console access?", "project"),
        ("A2", "Do you have processes in place to make sure access (both physical and logical) is revoked when an employee, "
               "intern, vendor, contractor, or other associate leaves the company or a project?", "VSAQ"),
        ("A3", "Are user access rights reviewed periodically?", "project"),
    ]),
    ("B. Data protection", [
        ("B1", "Is data at rest encrypted?", "project"),
        ("B2", "Is encryption and integrity protection in place for all internal network traffic that potentially carries "
               "sensitive information?", "VSAQ"),
        ("B3", "Are any storage buckets publicly accessible?", "project"),
    ]),
    ("C. Security operations", [
        ("C1", "Do you monitor your network for security events?", "VSAQ"),
        ("C2", "Do you have a process for installing operating system and application updates and security patches on "
               "servers?", "VSAQ"),
        ("C3", "Do you regularly test your backups?", "VSAQ"),
        ("C4", "Does an independent third party regularly perform penetration tests on all systems used to provide "
               "services to customers?", "VSAQ"),
    ]),
    ("D. People and incidents", [
        ("D1", "Do employees complete security awareness training?", "project"),
        ("D2", "Are incident response procedures documented?", "VSAQ"),
        ("D3", "Do you inform affected customers about security incidents?", "VSAQ"),
        ("D4", "Is customer data stored in the EU only?", "project"),
    ]),
]


def build_pdf(path: Path) -> None:
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=9, leading=11)
    cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=9.5, leading=12)
    story = [Paragraph(f"{BANK}: Third-party security assessment", styles["Title"]),
             Paragraph("Supplier: ______________________ &nbsp;&nbsp; Completed by: ______________________ &nbsp;&nbsp; "
                       "Date: __________", small), Spacer(1, 8),
             Paragraph("<b>Instructions.</b> Answer every question Yes, No, Partial or N/A, and explain any answer other "
                       "than Yes in the Comments column. Attach supporting evidence where available. Return the completed "
                       "form within 15 business days.", small), Spacer(1, 10)]
    for n, (title, questions) in enumerate(SECTIONS):
        if n == 2:
            story.append(PageBreak())
        story.append(Paragraph(title, styles["Heading3"]))
        rows = [["Ref", "Question", "Answer", "Comments"]] + [[ref, Paragraph(text, cell), "", ""] for ref, text, _ in questions]
        t = Table(rows, colWidths=[32, 300, 55, 110], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                               ("FONTSIZE", (0, 0), (-1, -1), 9.5), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [t, Spacer(1, 12)]
    story.append(Paragraph("Thank you. Questions about this form: vendor-risk@northwind.example", small))
    SimpleDocTemplate(str(path), pagesize=A4, title=f"{BANK} security assessment", leftMargin=40, rightMargin=40).build(story)


def screenshot(pdf: Path, png: Path) -> None:
    import pypdfium2 as pdfium
    page = pdfium.PdfDocument(str(pdf))[0]
    page.render(scale=1.6).to_pil().convert("RGB").save(png, optimize=True)


def main() -> None:
    pdf, png = ROOT / "samples" / "questionnaire.pdf", ROOT / "samples" / "questionnaire-page1.png"
    build_pdf(pdf)
    screenshot(pdf, png)
    truth = {"format": "vea.intake-truth.v1",
             "description": f"The questions in samples/questionnaire.pdf ({BANK}); page 1 holds sections A and B, which are "
                            "also the questions in samples/questionnaire-page1.png.",
             "questions": [{"ref": ref, "section": title, "text": text, "page": 1 if n < 2 else 2, "source": src}
                           for n, (title, qs) in enumerate(SECTIONS) for ref, text, src in qs]}
    (ROOT / "samples" / "questionnaire-truth.json").write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {pdf.name}, {png.name} and questionnaire-truth.json ({len(truth['questions'])} questions)")


if __name__ == "__main__":
    main()
