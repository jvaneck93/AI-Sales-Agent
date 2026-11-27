"""An Excel template for the lead import, with an explanation sheet. Fictional examples."""

import io

COLUMNS = [
    # (column header, required, explanation)
    ("Bedrijfsnaam", True, "Naam van het bedrijf."),
    (
        "KvK-nummer",
        True,
        "8 cijfers. Zet de kolom in Excel op 'Tekst' zodat voorloopnullen blijven.",
    ),
    (
        "Rechtsvorm",
        False,
        "Bijvoorbeeld B.V., N.V., Eenmanszaak. Alleen bv en nv worden koud gebeld; "
        "leeg of onbekend = niet bellen tot de KvK-check.",
    ),
    (
        "Adres",
        False,
        "Straat en huisnummer. Losse kolommen Straat, Huisnummer en Postcode mogen ook.",
    ),
    ("Plaats", False, "Vestigingsplaats."),
    ("Sector", False, "Branche of SBI-omschrijving. Wordt gebruikt voor de selectie per merk."),
    (
        "Telefoon",
        False,
        "Algemeen nummer van het bedrijf. Zonder nummer kan er niet gebeld worden.",
    ),
    ("E-mail", False, "Algemeen e-mailadres."),
    ("Website", False, "Voor de research."),
    ("Contactpersoon", False, "Naam van de beslisser over het wagenpark."),
    ("Functie", False, "Bijvoorbeeld wagenparkbeheerder of directeur."),
    ("Telefoon contactpersoon", False, "Direct nummer. Persoonsgegeven: alleen als het mag."),
    ("E-mail contactpersoon", False, "Voor de bevestiging van de afspraak."),
    ("Kenteken", False, "Eén voertuig per regel; herhaal dan de bedrijfskolommen."),
    ("Merk", False, "Merk van het voertuig."),
    ("Model", False, "Model van het voertuig."),
    ("Bouwjaar", False, "Jaar, of de datum eerste toelating."),
    ("Koop of lease", False, "koop of lease."),
    ("Leasemaatschappij", False, "Bij lease."),
    ("Einddatum contract", False, "Datum (dd-mm-jjjj of jjjj-mm-dd)."),
]

EXAMPLES = [
    [
        "Voorbeeld Installatie B.V.",
        "01234567",
        "B.V.",
        "Industrieweg 1",
        "Zwolle",
        "Installatietechniek",
        "038-1234567",
        "info@voorbeeld.example",
        "www.voorbeeld.example",
        "Testpersoon Een",
        "Wagenparkbeheerder",
        "",
        "",
        "AB123C",
        "Fictiva",
        "Bestel",
        "2020",
        "lease",
        "Demo Lease",
        "2027-03-31",
    ],
    [
        "Voorbeeld Installatie B.V.",
        "01234567",
        "B.V.",
        "Industrieweg 1",
        "Zwolle",
        "Installatietechniek",
        "038-1234567",
        "info@voorbeeld.example",
        "www.voorbeeld.example",
        "",
        "",
        "",
        "",
        "XY987Z",
        "Fictiva",
        "Bestel",
        "2018",
        "koop",
        "",
        "",
    ],
    [
        "Demo Bouw N.V.",
        "76543210",
        "N.V.",
        "Havenkade 12",
        "Rotterdam",
        "Bouw",
        "010-1234567",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    ],
]


def build_template() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Leads"
    sheet.append([c[0] for c in COLUMNS])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in EXAMPLES:
        sheet.append(row)
    for index, (header, _, _) in enumerate(COLUMNS, start=1):
        letter = sheet.cell(row=1, column=index).column_letter
        sheet.column_dimensions[letter].width = max(12, len(header) + 2)
        if header == "KvK-nummer":
            for cell in sheet[letter]:
                cell.number_format = "@"  # text, keeps leading zeros
    sheet.freeze_panes = "A2"

    info = workbook.create_sheet("Uitleg")
    info.append(["Kolom", "Verplicht", "Uitleg"])
    for cell in info[1]:
        cell.font = Font(bold=True)
    for header, required, text in COLUMNS:
        info.append([header, "ja" if required else "nee", text])
    info.append([])
    info.append(
        [
            "",
            "",
            "Andere kolomnamen worden vaak automatisch herkend (bv. Handelsnaam, "
            "KvK, Vestigingsplaats). Anders: pas config/import_mapping.yaml aan.",
        ]
    )
    info.append(["", "", "De voorbeeldregels zijn verzonnen. Verwijder ze voor gebruik."])
    info.column_dimensions["A"].width = 26
    info.column_dimensions["B"].width = 10
    info.column_dimensions["C"].width = 100

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
