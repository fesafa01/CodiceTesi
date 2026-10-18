"""Rigenera draw.io UML, SVG e PNG delle componenti effettive dell'applicazione."""

import math
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/diagrams"
INK = "#243247"


def font(size, bold=False):
    names = [
        Path("/System/Library/Fonts/Supplemental") / ("Arial Bold.ttf" if bold else "Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu")
        / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]
    for file in names:
        if file.exists():
            return ImageFont.truetype(str(file), size)
    return ImageFont.load_default(size=size)


class Diagram:
    def __init__(self, name, kind, title, subtitle, height=1360):
        self.name, self.kind, self.width, self.height = name, kind, 1800, height
        self.image = Image.new("RGB", (self.width, height), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.svg = []
        self.model = ET.Element(
            "mxGraphModel",
            page="1",
            pageWidth=str(self.width),
            pageHeight=str(height),
            grid="1",
            gridSize="10",
        )
        self.root = ET.SubElement(self.model, "root")
        ET.SubElement(self.root, "mxCell", id="0")
        ET.SubElement(self.root, "mxCell", id="1", parent="0")
        self.nodes, self.count = {}, 0
        self.text(40, 25, title, size=34, bold=True)
        self.text(40, 78, subtitle, size=22)

    def uid(self):
        self.count += 1
        return f"text-{self.count}"

    def line(self, points, dashed=False, color=INK, width=2):
        coords = " ".join(f"{x},{y}" for x, y in points)
        dash = ' stroke-dasharray="8 6"' if dashed else ""
        self.svg.append(
            f'<polyline points="{coords}" fill="none" stroke="{color}" '
            f'stroke-width="{width}"{dash}/>'
        )
        if not dashed:
            self.draw.line(points, fill=color, width=width)
        else:
            for a, b in zip(points, points[1:]):
                distance = math.dist(a, b)
                for start in range(0, math.ceil(distance), 14):
                    end = min(start + 8, distance)
                    if distance:
                        self.draw.line(
                            [
                                (
                                    a[0] + (b[0] - a[0]) * v / distance,
                                    a[1] + (b[1] - a[1]) * v / distance,
                                )
                                for v in (start, end)
                            ],
                            fill=color,
                            width=width,
                        )

    def rect(self, x, y, w, h, fill="white"):
        self.svg.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" '
            f'stroke="{INK}" stroke-width="2"/>'
        )
        self.draw.rectangle((x, y, x + w, y + h), fill=fill, outline=INK, width=2)

    def cell(self, id_, x, y, w, h, label, style, type_, parent="1"):
        px, py = self.nodes[parent][:2] if parent != "1" else (0, 0)
        cell = ET.SubElement(
            self.root,
            "mxCell",
            id=id_,
            value=label,
            parent=parent,
            vertex="1",
            umlType=type_,
            style=style + "html=1;fontFamily=Arial;fontColor=" + INK + ";",
        )
        ET.SubElement(
            cell,
            "mxGeometry",
            x=str(x - px),
            y=str(y - py),
            width=str(w),
            height=str(h),
            attrib={"as": "geometry"},
        )
        self.nodes[id_] = (x, y, w, h)
        return cell

    def text(self, x, y, label, size=21, bold=False, w=None, center=False, parent="1"):
        lines = label.split("\n")
        width = w or max(font(size, bold).getlength(s) for s in lines) + 5
        for i, line in enumerate(lines):
            measured = font(size, bold).getlength(line)
            assert measured <= width, (self.name, line, measured, width)
            tx = x + (width - measured) / 2 if center else x
            yy = y + i * (size + 8)
            self.draw.text((tx, yy), line, font=font(size, bold), fill=INK)
            self.svg.append(
                f'<text x="{x + width / 2 if center else x}" y="{yy + size}" '
                f'font-family="Arial,sans-serif" font-size="{size}" '
                f'font-weight="{700 if bold else 400}" '
                f'text-anchor="{"middle" if center else "start"}" fill="{INK}">'
                f"{escape(line)}</text>"
            )
        self.cell(
            self.uid(),
            x,
            y,
            width,
            len(lines) * (size + 8),
            "<br>".join(escape(s) for s in lines),
            f"text;whiteSpace=wrap;align={'center' if center else 'left'};"
            f"verticalAlign=top;spacing=0;fontSize={size};fontStyle={1 if bold else 0};",
            "Comment" if parent == "1" else "Label",
            parent,
        )

    def package(self, id_, x, y, w, h, label):
        tab = min(w - 10, font(23, True).getlength(label) + 32)
        self.rect(x, y + 40, w, h - 40, "#f4f7fb")
        self.rect(x, y, tab, 40, "#f4f7fb")
        self.cell(
            id_,
            x,
            y,
            w,
            h,
            "",
            f"shape=folder;tabWidth={tab};tabHeight=40;container=1;collapsible=0;fillColor=#f4f7fb;",
            "Package",
        )
        self.text(x + 15, y + 8, label, 23, True, w=w - 30, parent=id_)

    def frame(self, id_, x, y, w, h, label, keyword, parent="1"):
        self.rect(x, y, w, h, "#f4f7fb")
        self.line(
            [
                (x, y),
                (x + 12, y - 12),
                (x + w + 12, y - 12),
                (x + w + 12, y + h - 12),
                (x + w, y + h),
            ]
        )
        self.line([(x + w, y), (x + w + 12, y - 12)])
        self.cell(
            id_,
            x,
            y,
            w,
            h,
            "",
            "rounded=0;container=1;collapsible=0;fillColor=#f4f7fb;",
            "Device" if keyword == "device" else "ExecutionEnvironment",
            parent,
        )
        self.text(x + 15, y + 12, f"«{keyword}» " + label, 23, True, w=w - 30, parent=id_)

    def box(self, id_, x, y, w, h, label, description="", type_="Component", parent="1"):
        self.rect(x, y, w, h)
        keyword = {
            "Component": "component",
            "Artifact": "artifact",
            "Node": "executionEnvironment",
        }[type_]
        self.cell(
            id_,
            x,
            y,
            w,
            h,
            "",
            "rounded=0;container=1;collapsible=0;fillColor=#ffffff;strokeWidth=2;",
            type_,
            parent,
        )
        self.text(x + 12, y + 10, f"«{keyword}»", 19, w=w - 24, center=True, parent=id_)
        self.text(x + 12, y + 42, label, 23, True, w=w - 24, center=True, parent=id_)
        if description:
            lines = label.count("\n") + 1
            self.text(
                x + 12, y + 48 + lines * 31, description, 19, w=w - 24, center=True, parent=id_
            )

    def edge(self, source, target, points, label=None, label_at=None, association=False):
        self.line(points, dashed=not association)
        if not association:
            a, b = points[-2:]
            angle = math.atan2(b[1] - a[1], b[0] - a[0])
            wings = [
                (b[0] - 14 * math.cos(angle - d), b[1] - 14 * math.sin(angle - d))
                for d in (-0.45, 0.45)
            ]
            self.line([wings[0], b, wings[1]])
        style = (
            f"edgeStyle=segmentEdgeStyle;rounded=0;html=1;strokeColor={INK};"
            f"strokeWidth=2;endArrow={'none' if association else 'open'};"
        )
        if not association:
            style += "dashed=1;"
        for key, pt, prefix in ((source, points[0], "exit"), (target, points[-1], "entry")):
            x, y, w, h = self.nodes[key]
            style += f"{prefix}X={(pt[0] - x) / w};{prefix}Y={(pt[1] - y) / h};{prefix}Perimeter=0;"
        cell = ET.SubElement(
            self.root,
            "mxCell",
            id=self.uid(),
            edge="1",
            parent="1",
            source=source,
            target=target,
            umlType="CommunicationPath" if association else "Dependency",
            style=style,
        )
        geometry = ET.SubElement(cell, "mxGeometry", relative="1", attrib={"as": "geometry"})
        array = ET.SubElement(geometry, "Array", attrib={"as": "points"})
        for x, y in points[1:-1]:
            ET.SubElement(array, "mxPoint", x=str(x), y=str(y))
        if label:
            self.text(*label_at, label, size=18)

    def save(self, stem):
        self.image.save(OUT / f"{stem}.png")
        (OUT / f"{stem}.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}">'
            '<rect width="100%" height="100%" fill="white"/>' + "".join(self.svg) + "</svg>"
        )


def components():
    p = Diagram(
        "Componenti applicative",
        "ComponentDiagram",
        "Applicazione attuale: componenti",
        "Stato del 17 ottobre 2026 · UML · "
        "Agente e backend sono due percorsi applicativi indipendenti",
    )
    p.package("agent", 40, 150, 430, 720, "Agente CLI")
    p.box("agent-cli", 70, 215, 370, 130, "Comandi tesi-agent", "main.py / cli.py", parent="agent")
    p.box(
        "pydantic-ai",
        70,
        415,
        370,
        130,
        "Agente PydanticAI",
        "Istruzioni e generazione",
        parent="agent",
    )
    p.box(
        "sdk",
        70,
        640,
        370,
        130,
        "Client OpenAI-compatible",
        "SDK e configurazione LLM",
        parent="agent",
    )
    p.package("clinical", 550, 150, 660, 720, "Backend clinico")
    p.box(
        "api",
        600,
        215,
        560,
        135,
        "API REST e FHIR R4B",
        "FastAPI, Uvicorn e console Swagger",
        parent="clinical",
    )
    p.box(
        "mapper",
        585,
        435,
        270,
        140,
        "Adattatore FHIR",
        "Risorse e validazione\nPydantic / fhir.resources",
        parent="clinical",
    )
    p.box(
        "repo",
        910,
        435,
        270,
        140,
        "Repository clinico",
        "Contesto, canali,\nversioni e visibilità",
        parent="clinical",
    )
    p.box(
        "orm",
        750,
        685,
        360,
        130,
        "Accesso PostgreSQL",
        "Pool Psycopg e query SQL",
        parent="clinical",
    )
    p.package("data", 1300, 150, 460, 720, "Persistenza e dati")
    p.box(
        "db",
        1350,
        435,
        360,
        155,
        "Database clinico",
        "Pazienti, episodi, fonti,\nmisure, PDF e simulazione",
        parent="data",
    )
    p.box(
        "dataset",
        1350,
        690,
        360,
        135,
        "Dataset esterno",
        "Importazione esplicita\nNessun caso distribuito",
        type_="Artifact",
        parent="data",
    )
    p.box(
        "llm",
        70,
        1010,
        370,
        145,
        "Server LLM remoto",
        "API su spark-413e\nConnessione via Tailscale",
    )
    p.box(
        "ops",
        570,
        1010,
        390,
        145,
        "CLI operativa backend",
        "Schema e importazione\nControllo degli scenari",
    )
    p.box(
        "tests",
        1330,
        1010,
        390,
        145,
        "Test e integrazione continua",
        "pytest, Ruff e workflow GitHub",
    )
    p.edge("agent-cli", "pydantic-ai", [(255, 345), (255, 415)])
    p.edge("pydantic-ai", "sdk", [(255, 545), (255, 640)])
    p.edge("sdk", "llm", [(255, 770), (255, 1010)], "HTTP / OpenAI", (275, 935))
    p.edge("api", "mapper", [(720, 350), (720, 435)])
    p.edge("api", "repo", [(1045, 350), (1045, 435)])
    p.edge("mapper", "repo", [(855, 505), (910, 505)])
    p.edge("repo", "orm", [(1045, 575), (1045, 685)])
    p.edge("orm", "db", [(1110, 750), (1250, 750), (1250, 520), (1350, 520)])
    p.edge("ops", "db", [(960, 1040), (1270, 1040), (1270, 385), (1530, 385), (1530, 435)])
    p.edge("ops", "dataset", [(960, 1120), (1230, 1120), (1230, 790), (1350, 790)])
    p.edge("tests", "api", [(1330, 1090), (1200, 1090), (1200, 385), (1110, 385), (1110, 350)])
    p.edge(
        "tests", "pydantic-ai", [(1410, 1155), (1410, 1220), (495, 1220), (495, 480), (440, 480)]
    )
    p.text(
        45,
        1270,
        "Dipendenza UML: tratteggio con freccia dal componente che usa a quello utilizzato.",
        21,
    )
    p.text(
        45,
        1305,
        "Il repository è riusabile dai futuri tool senza HTTP. "
        "Non è presente un’interfaccia clinica per il medico.",
        21,
    )
    return p


def deployment():
    p = Diagram(
        "Distribuzione Docker",
        "DeploymentDiagram",
        "Applicazione attuale: distribuzione",
        "UML · Configurazione Docker Compose · Servizi su richiesta "
        "e infrastruttura esterna sono indicati esplicitamente",
    )
    p.frame("host", 40, 150, 1240, 1040, "Host di sviluppo", "device")
    p.frame(
        "docker",
        70,
        220,
        1180,
        930,
        "Docker Engine / Compose",
        "executionEnvironment",
        parent="host",
    )
    p.box(
        "app",
        110,
        310,
        330,
        155,
        "Container app",
        "tesi-agent\nAvvio su richiesta",
        type_="Node",
        parent="docker",
    )
    p.box(
        "backend",
        570,
        310,
        380,
        170,
        "Container backend",
        "tesi-backend serve\nRuolo DB: clinical_reader",
        type_="Node",
        parent="docker",
    )
    p.box(
        "init",
        110,
        590,
        330,
        155,
        "Container backend-init",
        "Solo schema SQL\nNessuna importazione",
        type_="Node",
        parent="docker",
    )
    p.box(
        "control",
        110,
        900,
        330,
        155,
        "Container backend-control",
        "Importazione e scenari\nAvvio su richiesta",
        type_="Node",
        parent="docker",
    )
    p.box(
        "db",
        570,
        660,
        380,
        160,
        "Container db",
        "PostgreSQL 17\nAmministrazione: clinico",
        type_="Node",
        parent="docker",
    )
    p.box(
        "data",
        1000,
        660,
        220,
        155,
        "Input esterno",
        "Mount esplicito\nper importazione",
        type_="Artifact",
        parent="docker",
    )
    p.box(
        "volume",
        570,
        940,
        380,
        130,
        "Volume clinical-db",
        "Persistenza tra i riavvii",
        type_="Artifact",
        parent="docker",
    )
    p.box(
        "remote",
        1350,
        310,
        400,
        180,
        "Ambiente inferenza remoto",
        "spark-413e / server LLM\nAPI OpenAI-compatible",
        type_="Node",
    )
    p.box(
        "ci",
        1350,
        900,
        400,
        155,
        "Runner GitHub Actions",
        "Workflow configurato\nTest con PostgreSQL isolato",
        type_="Node",
    )
    p.edge(
        "app",
        "remote",
        [(440, 390), (495, 390), (495, 270), (1550, 270), (1550, 310)],
        "HTTP su Tailscale",
        (1360, 235),
        association=True,
    )
    p.edge(
        "backend",
        "db",
        [(760, 480), (760, 660)],
        "TCP 5432 / lettura",
        (780, 555),
        association=True,
    )
    p.edge("init", "db", [(440, 680), (510, 680), (510, 705), (570, 705)], association=True)
    p.edge("control", "db", [(440, 975), (510, 975), (510, 770), (570, 770)], association=True)
    p.edge("control", "data", [(440, 1020), (480, 1020), (480, 850), (1110, 850), (1110, 815)])
    p.edge("db", "volume", [(760, 820), (760, 940)], "Persistenza", (780, 870))
    p.text(805, 495, "HTTP: 127.0.0.1:8001", 19)
    p.text(
        45,
        1220,
        "Linea continua senza freccia: comunicazione tra ambienti. "
        "Tratteggio: dipendenza da un artefatto.",
        21,
    )
    p.text(
        45,
        1255,
        "Avvio senza dati clinici. PostgreSQL locale: 127.0.0.1:5433.",
        21,
    )
    p.text(
        45,
        1290,
        "Importazione su richiesta. I futuri tool potranno usare il repository "
        "senza HTTP; non sono ancora collegati.",
        21,
    )
    return p


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    pages = [components(), deployment()]
    document = ET.Element("mxfile", host="app.diagrams.net", agent="CodiceTesi UML", type="device")
    for i, (page, stem) in enumerate(
        zip(pages, ("Applicazione_Componenti", "Applicazione_Deployment")), 1
    ):
        item = ET.SubElement(
            document, "diagram", id=f"page-{i}", name=page.name, umlDiagramType=page.kind
        )
        item.append(page.model)
        page.save(stem)
    ET.indent(document, space="  ")
    (OUT / "Applicazione_Attuale.drawio").write_bytes(
        ET.tostring(document, encoding="utf-8", xml_declaration=True)
    )
    print("Diagrammi UML generati: due pagine draw.io, SVG e PNG.")


if __name__ == "__main__":
    main()
