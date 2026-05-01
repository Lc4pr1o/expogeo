# exporter.py
# Funções de exportação de linhas-guia para terminais agrícolas.

import os
import math
import unicodedata
import uuid
import json
import zipfile
import shutil
import struct as _struct
import base64
import gzip
import io
from datetime import datetime, timezone

from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsWkbTypes,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
)
from qgis.PyQt.QtCore import QVariant

DST_CRS = QgsCoordinateReferenceSystem("EPSG:4326")

# Limite por bloco — será configurável em versão futura
BLOCK_SIZE_LIMIT_MB = 2.5


def ascii_safe(s):
    s = str(s) if s and str(s) not in ("NULL", "None") else ""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn" and ord(c) < 128
    ).strip()

def new_guid():
    return str(uuid.uuid4())

def heading_deg(p0, p1):
    return (math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0])) + 360) % 360

def to_wgs84_xform(layer):
    src = layer.crs() if layer.crs().isValid() else QgsCoordinateReferenceSystem("EPSG:32723")
    if src.authid() == "EPSG:4326":
        return None
    return QgsCoordinateTransform(src, DST_CRS, QgsProject.instance())

def geom_to_latlon(geom, tr):
    """Retorna lista de (lat, lon) em WGS84."""
    pts = []
    raw = (
        [p for part in geom.asMultiPolyline() for p in part]
        if geom.isMultipart() else geom.asPolyline()
    )
    for p in raw:
        if tr:
            q = tr.transform(QgsPointXY(p))
            pts.append((q.y(), q.x()))
        else:
            pts.append((p.y(), p.x()))
    return pts

def count_verts(geom):
    if geom.isMultipart():
        return sum(len(p) for p in geom.asMultiPolyline())
    return len(geom.asPolyline())

def get_attr(feat, name, default=""):
    try:
        v = feat[name]
        return str(v) if v and str(v) not in ("NULL", "None") else default
    except Exception:
        return default


# ── Coleta feições da camada ──────────────────────────────────

def collect_lines(layer):
    tr = to_wgs84_xform(layer)
    out = []
    for feat in layer.getFeatures():
        g = feat.geometry()
        if not g or g.isEmpty():
            continue
        pts = geom_to_latlon(g, tr)
        if len(pts) < 2:
            continue
        out.append({
            "fid":     feat.id(),
            "name":    get_attr(feat, "talhao") or get_attr(feat, "fazenda") or f"L{feat.id()}",
            "cliente": get_attr(feat, "cliente"),
            "fazenda": get_attr(feat, "fazenda"),
            "talhao":  get_attr(feat, "talhao"),
            "tipo":    get_attr(feat, "tipo_linha", "Curva"),
            "pts":     pts,          # lista de (lat, lon)
        })
    return out


# ── Estimativa de tamanho e divisão em blocos ─────────────────

def estimate_lines_size_mb(lines):
    """Estimativa de tamanho baseada em vértices (16 bytes cada) + overhead."""
    total_verts = sum(len(gl['pts']) for gl in lines)
    return (total_verts * 16 + len(lines) * 100) / (1024 * 1024)


def estimate_layer_size_mb(layer):
    """Estimativa de tamanho do shapefile baseada nos vértices da camada."""
    total_bytes = 0
    for feat in layer.getFeatures():
        g = feat.geometry()
        if g and not g.isEmpty():
            total_bytes += count_verts(g) * 16 + 100
    return total_bytes / (1024 * 1024)


def _predominant_angle(lines_group):
    """Ângulo médio predominante de um grupo de linhas (circular mean)."""
    angles = []
    for gl in lines_group:
        if len(gl['pts']) >= 2:
            p0, p1 = gl['pts'][0], gl['pts'][-1]
            a = math.atan2(p1[1] - p0[1], p1[0] - p0[0]) % math.pi
            angles.append(a)
    if not angles:
        return 0.0
    sin_avg = sum(math.sin(2 * a) for a in angles) / len(angles)
    cos_avg = sum(math.cos(2 * a) for a in angles) / len(angles)
    return math.atan2(sin_avg, cos_avg) / 2


def split_into_blocks(lines, nomenclature, limit_mb=None):
    """
    Divide linhas em blocos de até limit_mb MB.
    - Agrupa por talhão, ordena por ângulo predominante.
    - Talhões que cabem no limite ficam inteiros num bloco.
    - Talhões maiores que o limite são sub-divididos sequencialmente.
    Retorna: lista de dicts {name, talhoes, lines, size_mb}
    """
    if limit_mb is None:
        limit_mb = BLOCK_SIZE_LIMIT_MB

    # Agrupa por talhão
    talhao_map = {}
    for gl in lines:
        key = gl['talhao'] or gl['fazenda'] or gl['name'] or 'SEM_TALHAO'
        talhao_map.setdefault(key, []).append(gl)

    # Ordena por ângulo predominante
    sorted_talhoes = sorted(talhao_map.items(), key=lambda kv: _predominant_angle(kv[1]))

    # Segmentos atômicos: talhões normais ficam inteiros; grandes são sub-divididos
    segments = []
    for key, grp_lines in sorted_talhoes:
        grp_mb = estimate_lines_size_mb(grp_lines)
        if grp_mb <= limit_mb:
            segments.append({'label': key, 'lines': grp_lines, 'mb': grp_mb})
        else:
            chunk, chunk_mb = [], 0.0
            for gl in grp_lines:
                gl_mb = estimate_lines_size_mb([gl])
                if chunk and chunk_mb + gl_mb > limit_mb:
                    segments.append({'label': key, 'lines': chunk, 'mb': chunk_mb})
                    chunk, chunk_mb = [], 0.0
                chunk.append(gl)
                chunk_mb += gl_mb
            if chunk:
                segments.append({'label': key, 'lines': chunk, 'mb': chunk_mb})

    # Empacota segmentos em blocos
    blocks = []
    cur_talhoes, cur_lines, cur_mb = [], [], 0.0

    for seg in segments:
        if cur_lines and cur_mb + seg['mb'] > limit_mb:
            blocks.append({'talhoes': list(cur_talhoes), 'lines': list(cur_lines),
                           'size_mb': cur_mb})
            cur_talhoes, cur_lines, cur_mb = [], [], 0.0
        cur_talhoes.append(seg['label'])
        cur_lines.extend(seg['lines'])
        cur_mb += seg['mb']

    if cur_lines:
        blocks.append({'talhoes': list(cur_talhoes), 'lines': list(cur_lines),
                       'size_mb': cur_mb})

    for i, b in enumerate(blocks, 1):
        b['name'] = f'{nomenclature} {i}'

    return blocks


# ── Padronização de espaçamento de vértices ───────────────────

def _chord_deviation(pt, a, b):
    """Distância perpendicular de pt até o segmento a→b."""
    dx = b.x() - a.x()
    dy = b.y() - a.y()
    len_sq = dx * dx + dy * dy
    if len_sq == 0:
        return math.hypot(pt.x() - a.x(), pt.y() - a.y())
    t = max(0.0, min(1.0, ((pt.x() - a.x()) * dx + (pt.y() - a.y()) * dy) / len_sq))
    return math.hypot(pt.x() - (a.x() + t * dx), pt.y() - (a.y() + t * dy))


def _regularize_points(pts, min_d, max_d, dev_tol):
    """
    Normaliza o espaçamento entre vértices:
    - Remove vértices < min_d do anterior SOMENTE se o desvio geométrico
      (distância do ponto à corda anterior→próximo) for <= dev_tol.
      Se o ponto estiver numa curva (desvio > dev_tol), é mantido.
    - Insere vértices interpolados onde o gap > max_d (densificação).
      A densificação nunca altera o traçado.
    """
    if len(pts) < 2:
        return list(pts)

    result = [pts[0]]
    last = pts[0]

    for i in range(1, len(pts) - 1):
        pt = pts[i]
        dx = pt.x() - last.x()
        dy = pt.y() - last.y()
        d = math.hypot(dx, dy)

        if d < min_d:
            # Candidato a remoção — verifica se está numa curva
            dev = _chord_deviation(pt, last, pts[i + 1])
            if dev <= dev_tol:
                continue  # trecho reto: descarta com segurança
            # Curva relevante: mantém o ponto mesmo estando próximo

        if d > max_d:
            n = math.ceil(d / max_d)
            for j in range(1, n):
                t = j / n
                result.append(QgsPointXY(last.x() + t * dx, last.y() + t * dy))

        result.append(pt)
        last = pt

    # Último ponto: sempre incluído; densifica se necessário
    pt = pts[-1]
    dx = pt.x() - last.x()
    dy = pt.y() - last.y()
    d = math.hypot(dx, dy)
    if d > max_d:
        n = math.ceil(d / max_d)
        for j in range(1, n):
            t = j / n
            result.append(QgsPointXY(last.x() + t * dx, last.y() + t * dy))
    result.append(pt)
    return result


def simplify_layer(layer, min_dist, max_dist, dev_tol, progress_cb=None):
    """
    Regulariza o espaçamento de vértices de todas as feições.
    Parâmetros em metros (requer CRS projetado — ex: UTM).
    A geometria nunca é distorcida; apenas o espaçamento entre vértices
    é normalizado para ficar entre min_dist e max_dist metros.
    """
    mem = QgsVectorLayer(
        f"LineString?crs={layer.crs().authid()}",
        f"{layer.name()}_simpl", "memory")
    pr = mem.dataProvider()
    pr.addAttributes(layer.fields().toList())
    existing = [f.name() for f in layer.fields()]
    extras = [QgsField(fn, QVariant.String, "String", 100)
              for fn in ["cliente", "fazenda", "talhao", "tipo_linha"]
              if fn not in existing]
    if extras:
        pr.addAttributes(extras)
    mem.updateFields()

    total = layer.featureCount()
    vb = va = 0
    feats_out = []

    for i, feat in enumerate(layer.getFeatures()):
        g = feat.geometry()
        if not g or g.isEmpty():
            continue
        vb += count_verts(g)

        if g.isMultipart():
            parts = g.asMultiPolyline()
            new_parts = [_regularize_points(p, min_dist, max_dist, dev_tol) for p in parts]
            new_parts = [p for p in new_parts if len(p) >= 2]
            if not new_parts:
                continue
            sg = QgsGeometry.fromMultiPolylineXY(new_parts)
        else:
            pts = g.asPolyline()
            new_pts = _regularize_points(pts, min_dist, max_dist, dev_tol)
            if len(new_pts) < 2:
                continue
            sg = QgsGeometry.fromPolylineXY(new_pts)

        v = count_verts(sg)
        va += v

        nf = QgsFeature(mem.fields())
        nf.setGeometry(sg)
        for fld in layer.fields():
            nf[fld.name()] = feat[fld.name()]
        for fn in ["cliente", "fazenda", "talhao"]:
            if fn not in existing:
                nf[fn] = ""
        if "tipo_linha" not in existing:
            nf["tipo_linha"] = "AB" if v == 2 else "Curva"
        feats_out.append(nf)

        if progress_cb and total > 0:
            progress_cb(int((i + 1) / total * 100))

    pr.addFeatures(feats_out)
    mem.updateExtents()
    pct = (1 - va / vb) * 100 if vb else 0
    return mem, {
        "features": len(feats_out), "before": vb, "after": va, "pct": pct,
    }


# ── Exportadores ──────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _today_local():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "-03:00"


# ── 1) Formato John Deere Operations Center (Gen4 .zip) ───────
#
#  Estrutura exata esperada pelo OC:
#
#  {prefix}.zip
#  └── Gen4/
#      ├── MasterData.xml
#      └── SpatialFiles/
#          └── AdaptiveCurve{guid}.gjson   ← uma por grupo
#
#  Cada grupo = linhas do mesmo (cliente, fazenda, talhao, nome)
#  O .gjson tem geometry.type = "Feature" / geometry.type = "MultiLineString"
#  com coordenadas [lon, lat, elev, 1]

def _gjson_for_group(lines_group):
    """Gera o conteúdo do .gjson para um grupo de linhas."""
    multiline_coords = []
    for gl in lines_group:
        # [lon, lat, elevation=0, flag=1]
        line_coords = [[round(lon, 10), round(lat, 10), 0, 1] for lat, lon in gl["pts"]]
        multiline_coords.append(line_coords)

    return {
        "type": "Feature",
        "geometry": {
            "type": "MultiLineString",
            "coordinates": multiline_coords
        }
    }


def _master_data_xml(groups, client_guid, farm_guid, field_guid,
                     client_name, farm_name, field_name, now_str):
    """Gera o MasterData.xml com todos os grupos de curvas."""

    lines = []
    lines.append('<?xml version="1.0" encoding="utf-8"?>')
    lines.append('<SetupFile xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                 'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
                 'xmlns="urn:schemas-johndeere-com:Setup">')
    lines.append('  <SourceApp minor="0" major="0" build="0" revision="0" '
                 'nameSourceApp="AgroExport-QGIS" SourceAppClientId="" />')
    lines.append('  <FileSchemaVersion nonProductionCode="0">')
    lines.append('    <FileSchemaContentVersion major="2" minor="19" />')
    lines.append('    <UnitOfMeasureVersion major="1" minor="8" />')
    lines.append('    <RepresentationSystemVersion major="3" minor="13" />')
    lines.append('  </FileSchemaVersion>')
    lines.append('  <Setup>')
    lines.append(f'    <Client Archived="false" StringGuid="{client_guid}" Name="{ascii_safe(client_name)}" />')
    lines.append(f'    <Farm LastModifiedDate="{now_str}" Archived="false" '
                 f'StringGuid="{farm_guid}" Name="{ascii_safe(farm_name)}" '
                 f'Client="{client_guid}" />')
    lines.append(f'    <Field LastModifiedDate="{now_str}" Archived="false" '
                 f'StringGuid="{field_guid}" Name="{ascii_safe(field_name)}">')
    lines.append(f'      <Farm>{farm_guid}</Farm>')
    lines.append('    </Field>')
    lines.append('    <Inputs><Products /></Inputs>')

    lines.append('    <Guidance>')
    lines.append('      <Tracks>')
    for grp in groups:
        g = grp["guid"]
        # Compute bounding box
        all_lats = [lat for gl in grp["lines"] for lat, lon in gl["pts"]]
        all_lons = [lon for gl in grp["lines"] for lat, lon in gl["pts"]]
        ref_lat  = round(all_lats[0], 13)
        ref_lon  = round(all_lons[0], 13)

        gjson_filename = f"AdaptiveCurve{g}.gjson"
        lines.append(f'        <AdaptiveCurve Archived="false" StringGuid="{g}" '
                     f'Name="{ascii_safe(grp["name"])}" '
                     f'TaggedEntity="{field_guid}">')
        lines.append('          <SignalType Representation="dtSignalType" Value="dtiSignalTypeUnknown" />')
        lines.append('          <Geometry>')
        lines.append(f'            <FilenameWithExtension>{gjson_filename}</FilenameWithExtension>')
        lines.append('            <Path>./SpatialFiles/</Path>')
        lines.append('          </Geometry>')
        lines.append('          <SpatialProjection>')
        lines.append('            <ProjectionType Representation="dtProjectionType" Value="dtiProjectionDeere" />')
        lines.append('            <ElevationReferencePoint Representation="vrElevation" Value="0" SourceUnit="m" />')
        lines.append('          </SpatialProjection>')
        lines.append(f'          <ReferenceLatitude Representation="vrLatitude" '
                     f'Value="{ref_lat}" SourceUnit="arcdeg" />')
        lines.append(f'          <ReferenceLongitude Representation="vrLongitude" '
                     f'Value="{ref_lon}" SourceUnit="arcdeg" />')
        lines.append('        </AdaptiveCurve>')
    lines.append('      </Tracks>')
    lines.append('    </Guidance>')
    lines.append(f'    <CreatedDateTime>{now_str}</CreatedDateTime>')
    lines.append('  </Setup>')
    lines.append('</SetupFile>')
    return "\r\n".join(lines)


def export_jd_zip(lines, output_dir, prefix,
                  client_name, farm_name, field_name):
    """
    Gera o pacote .zip no formato Operations Center.

    Agrupa as linhas pelo campo 'talhao' (ou 'fazenda' como fallback).
    Cada grupo único vira um AdaptiveCurve separado no MasterData.xml
    e um .gjson em SpatialFiles/.
    """
    if not lines:
        raise ValueError("Nenhuma feição válida para exportar.")

    # GUIDs fixos para cliente/fazenda/campo
    client_guid = new_guid()
    farm_guid   = new_guid()
    field_guid  = new_guid()
    now_str     = _today_local()

    # Agrupa por nome de orientação (talhao ou nome da linha)
    groups_dict = {}
    for gl in lines:
        key = gl["talhao"] or gl["fazenda"] or gl["name"] or "CURVA"
        if key not in groups_dict:
            groups_dict[key] = {"name": key.upper(), "guid": new_guid(), "lines": []}
        groups_dict[key]["lines"].append(gl)
    groups = list(groups_dict.values())

    gen4_dir    = os.path.join(output_dir, "Gen4")
    spatial_dir = os.path.join(gen4_dir, "SpatialFiles")
    os.makedirs(spatial_dir, exist_ok=True)

    # MasterData.xml
    xml_content = _master_data_xml(
        groups, client_guid, farm_guid, field_guid,
        client_name, farm_name, field_name, now_str
    )
    with open(os.path.join(gen4_dir, "MasterData.xml"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(xml_content)

    # Um .gjson por grupo
    for grp in groups:
        gjson_data = _gjson_for_group(grp["lines"])
        fname = f"AdaptiveCurve{grp['guid']}.gjson"
        with open(os.path.join(spatial_dir, fname), "w", encoding="utf-8") as f:
            json.dump(gjson_data, f, separators=(",", ":"))

    return gen4_dir, len(lines), len(groups)


# ── 2) Formato Trimble AgGPS (Shapefile WGS84) ───────────────
#
#  Usado por: Trimble AgGPS 542/252, EZ-Guide 500, CFX-750, FMX, TMX-2050
#
#  Estrutura:
#  AgGPS/
#  └── Data/
#      └── {client}/
#          └── {farm}/
#              └── {field}/
#                  ├── LineFeature.shp   (shape type 3 = Polyline)
#                  ├── LineFeature.shx   (index)
#                  ├── LineFeature.dbf   (atributos: Id, Name, Length, Dist1, Dist2)
#                  ├── LineFeature.prj   (WKT WGS84)
#                  └── {lon}E{lat}N600H.pos  (arquivo vazio — ponto de referência)

import struct as _struct
import math as _math

_PRJ_WGS84 = (
    'GEOGCS["GCS_WGS_1984",'
    'DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137,298.2572235629972]],'
    'PRIMEM["Greenwich",0],'
    'UNIT["Degree",0.017453292519943295]]'
)

def _line_length_deg(pts):
    """Comprimento aproximado em graus (usado no campo Length do DBF)."""
    total = 0.0
    for i in range(1, len(pts)):
        dlat = pts[i][0] - pts[i-1][0]
        dlon = pts[i][1] - pts[i-1][1]
        total += _math.sqrt(dlat*dlat + dlon*dlon)
    return total

def _write_shp_shx(lines_wgs, shp_path, shx_path):
    """
    Escreve SHP + SHX para uma lista de linhas.
    lines_wgs: lista de listas de (lat, lon) — convertido para (x=lon, y=lat)
    """
    # Pré-calcula bboxes por registro
    records = []
    for pts in lines_wgs:
        xs = [p[1] for p in pts]  # lon → X
        ys = [p[0] for p in pts]  # lat → Y
        records.append({
            'pts': pts,
            'bbox': (min(xs), min(ys), max(xs), max(ys)),
        })

    # bbox global
    g_xmin = min(r['bbox'][0] for r in records)
    g_ymin = min(r['bbox'][1] for r in records)
    g_xmax = max(r['bbox'][2] for r in records)
    g_ymax = max(r['bbox'][3] for r in records)

    shp_records = []
    for r in records:
        npts = len(r['pts'])
        # content: 4(type) + 32(bbox) + 4(nparts) + 4(npts) + 4(part0) + 16*npts
        content_len = 4 + 32 + 4 + 4 + 4 + 16 * npts
        content_words = content_len // 2
        buf = _struct.pack('<I4dII I',
            3,                          # shape type Polyline
            r['bbox'][0], r['bbox'][1], r['bbox'][2], r['bbox'][3],
            1, npts,                    # 1 part, N points
            0,                          # part[0] offset
        )
        for lat, lon in r['pts']:
            buf += _struct.pack('<2d', lon, lat)
        shp_records.append((content_words, buf))

    # SHP file length in 16-bit words: 50 (header) + sum(4 + content_words per rec)
    shp_file_len_words = 50 + sum(4 + cw for cw, _ in shp_records)

    with open(shp_path, 'wb') as shp, open(shx_path, 'wb') as shx:
        # SHP header (100 bytes)
        shp_hdr = _struct.pack('>I', 9994) + b'\x00' * 20
        shp_hdr += _struct.pack('>I', shp_file_len_words)
        shp_hdr += _struct.pack('<II', 1000, 3)  # version, shape type
        shp_hdr += _struct.pack('<8d', g_xmin, g_ymin, g_xmax, g_ymax, 0, 0, 0, 0)
        shp.write(shp_hdr)

        # SHX header (same size, file length = 50 + 4*nrecords)
        shx_file_len_words = 50 + 4 * len(records)
        shx_hdr = _struct.pack('>I', 9994) + b'\x00' * 20
        shx_hdr += _struct.pack('>I', shx_file_len_words)
        shx_hdr += _struct.pack('<II', 1000, 3)
        shx_hdr += _struct.pack('<8d', g_xmin, g_ymin, g_xmax, g_ymax, 0, 0, 0, 0)
        shx.write(shx_hdr)

        offset_words = 50  # current offset in SHP (words)
        for i, (cw, buf) in enumerate(shp_records):
            rec_num = i + 1
            # SHP record header
            shp.write(_struct.pack('>2I', rec_num, cw))
            shp.write(buf)
            # SHX record
            shx.write(_struct.pack('>2I', offset_words, cw))
            offset_words += 2 + cw  # 2 words header + content


def _write_dbf(lines_data, path):
    """
    DBF com campos: Id(N11), Name(C255), Length(N18.9), Dist1(N18.9), Dist2(N18.9)
    """
    fields = [
        (b'Id',     b'N', 11, 0),
        (b'Name',   b'C', 255, 0),
        (b'Length', b'N', 18, 9),
        (b'Dist1',  b'N', 18, 9),
        (b'Dist2',  b'N', 18, 9),
    ]
    record_size = 1 + sum(f[2] for f in fields)  # 1 = deletion flag
    num_fields  = len(fields)
    header_size = 32 + 32 * num_fields + 1        # file header + field descriptors + terminator

    now = datetime.now()
    with open(path, 'wb') as f:
        # File header (32 bytes)
        f.write(bytes([3, now.year - 1900, now.month, now.day]))
        f.write(_struct.pack('<I', len(lines_data)))  # num records
        f.write(_struct.pack('<HH', header_size, record_size))
        f.write(b'\x00' * 20)

        # Field descriptors (32 bytes each)
        for fname, ftype, flen, fdec in fields:
            fd = fname.ljust(11, b'\x00')[:11]
            fd += ftype + b'\x00' * 4
            fd += bytes([flen, fdec]) + b'\x00' * 14
            f.write(fd)
        f.write(b'\r')  # header terminator

        # Records
        for i, gl in enumerate(lines_data):
            name_val = ascii_safe(gl['talhao'] or gl['name'] or 'CURVA').upper()
            length_m = _line_length_deg(gl['pts'])
            # Convert length from degrees to approximate meters
            length_m_real = 0.0
            pts = gl['pts']
            for j in range(1, len(pts)):
                dlat = (pts[j][0] - pts[j-1][0]) * 111320
                dlon = (pts[j][1] - pts[j-1][1]) * 111320 * _math.cos(_math.radians(pts[j][0]))
                length_m_real += _math.sqrt(dlat*dlat + dlon*dlon)

            f.write(b' ')  # deletion flag (space = not deleted)
            f.write(str(i + 1).rjust(11).encode('ascii'))       # Id
            f.write(name_val.ljust(255).encode('ascii', 'replace')[:255])  # Name
            f.write(f'{length_m_real:.9f}'.rjust(18).encode('ascii'))      # Length
            f.write(b'0.000000000'.rjust(18))                   # Dist1
            f.write(b'0.000000000'.rjust(18))                   # Dist2

        f.write(b'\x1a')  # EOF marker


def export_aggps_zip(lines, output_dir, prefix, client_name, farm_name, field_name):
    """
    Gera pacote AgGPS .zip:
    AgGPS/Data/{client}/{farm}/{field}/LineFeature.{shp,shx,dbf,prj} + .pos
    """
    if not lines:
        raise ValueError("Nenhuma feição válida.")

    c = ascii_safe(client_name) or 'Cliente'
    f = ascii_safe(farm_name)   or 'Fazenda'
    t = ascii_safe(field_name)  or 'Talhao'

    fld_dir  = os.path.join(output_dir, 'AgGPS', 'Data', c, f, t)
    os.makedirs(fld_dir, exist_ok=True)

    # Shapefile
    pts_all = [gl['pts'] for gl in lines]
    _write_shp_shx(pts_all,
                   os.path.join(fld_dir, 'LineFeature.shp'),
                   os.path.join(fld_dir, 'LineFeature.shx'))
    _write_dbf(lines, os.path.join(fld_dir, 'LineFeature.dbf'))

    with open(os.path.join(fld_dir, 'LineFeature.prj'), 'w', encoding='ascii') as pf:
        pf.write(_PRJ_WGS84)

    # .pos file (empty — name encodes reference point)
    all_lats = [p[0] for gl in lines for p in gl['pts']]
    all_lons = [p[1] for gl in lines for p in gl['pts']]
    ref_lat  = sum(all_lats) / len(all_lats)
    ref_lon  = sum(all_lons) / len(all_lons)
    pos_name = f'{abs(ref_lon):.5f}E{abs(ref_lat):.5f}N600H.pos'
    open(os.path.join(fld_dir, pos_name), 'wb').close()

    return os.path.join(output_dir, 'AgGPS')


# ── 3) Formato GS3_2630 ───────────────────────────────────────
#
#  Estrutura:
#  GS3_2630/
#  └── {client}/
#      └── RCD/
#          └── EIC/
#              ├── setup.fds               (toda a frota — copiado/mínimo)
#              ├── global.ver              (1024 bytes: 0x07 + 0xFF * 1023)
#              ├── host                    (32 bytes aleatórios)
#              └── Fields/
#                  └── {hex2}/
#                      └── {field_guid}/
#                          ├── ImportExport.SpatialCatalog  (contém CurvedTrackLine)
#                          ├── ExportOnly.SpatialCatalog    (vazio)
#                          └── WaterManagement.SpatialCatalog (vazio)
#
#  Nota: CurveTrack*.fdShape é gerado pelo próprio terminal ao sincronizar.
#  O que o terminal lê na importação é o SpatialCatalog.

def _spatial_catalog_import_export(client_guid, farm_guid, field_guid,
                                   curve_guid, track_name,
                                   client_name, farm_name, field_name,
                                   bounds, now_str, session_uuid, node_uuid):
    """Gera o ImportExport.SpatialCatalog com o CurvedTrackLine."""
    n, s, e, w = bounds['north'], bounds['south'], bounds['east'], bounds['west']
    ref_lat = (n + s) / 2
    ref_lon = (e + w) / 2

    lines = []
    lines.append('<?xml version="1.0" encoding="utf-8"?>')
    lines.append(
        '<rcdscfldie:SpatialCatalog '
        'xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" '
        'xmlns:bt="urn:schemas-johndeere-com:BasicTypes" '
        'xmlns:rep="urn:schemas-johndeere-com:Representation" '
        'xmlns:unit="urn:schemas-johndeere-com:UnitSystem" '
        'xmlns:rcdscbase="urn:schemas-johndeere-com:RCD:SpatialCatalog:Base" '
        'xmlns:rcdsetup="urn:schemas-johndeere-com:RCD:Setup" '
        'xmlns:rcdscfldie="urn:schemas-johndeere-com:RCD:SpatialCatalog:FieldImportExport">'
    )
    lines.append('  <FileSchemaVersion nonProductionCode="0">')
    lines.append('    <bt:FileSchemaContentVersion major="1" minor="11" />')
    lines.append('    <bt:UnitOfMeasureVersion major="1" minor="43" />')
    lines.append('    <bt:RepresentationSystemVersion major="4" minor="161" />')
    lines.append('  </FileSchemaVersion>')
    lines.append(
        f'  <SourceApp major="3" minor="0" build="0" revision="117" '
        f'nameSourceApp="RCD Target Provider" '
        f'uuidSourceApp="{{b050528e-f328-4dd8-9e9c-71fe8153692e}}" '
        f'uuidSourceAppNode="{{{node_uuid}}}" '
        f'uuidSession="{{{session_uuid}}}" />'
    )
    lines.append('  <Setup>')
    lines.append('    <rcdsetup:FileSchemaVersion nonProductionCode="0">')
    lines.append('      <bt:FileSchemaContentVersion major="3" minor="28" />')
    lines.append('      <bt:UnitOfMeasureVersion major="1" minor="43" />')
    lines.append('      <bt:RepresentationSystemVersion major="4" minor="161" />')
    lines.append('    </rcdsetup:FileSchemaVersion>')
    lines.append('    <bt:Synchronization>')
    lines.append('      <bt:NodeVersions>')
    lines.append(f'        <bt:Node uuid="{{{node_uuid}}}" lastSeen="{now_str}" />')
    lines.append('      </bt:NodeVersions>')
    lines.append('      <bt:EntityDeletions />')
    lines.append('    </bt:Synchronization>')
    lines.append('    <rcdsetup:Participant>')
    lines.append(
        f'      <rcdsetup:Client lastModified="1970-01-01T00:00:00" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{client_guid}}}" '
        f'name="{ascii_safe(client_name)}" />'
    )
    lines.append('    </rcdsetup:Participant>')
    lines.append(
        f'    <rcdsetup:Farm lastModified="{now_str}" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{farm_guid}}}" '
        f'name="{ascii_safe(farm_name)}" clientRef="{{{client_guid}}}" />'
    )
    lines.append(
        f'    <rcdsetup:Field lastModified="{now_str}" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{field_guid}}}" '
        f'name="{ascii_safe(field_name)}" farmRef="{{{farm_guid}}}" />'
    )
    lines.append('    <rcdsetup:Products />')
    lines.append('  </Setup>')
    lines.append(f'  <SpatialItems eridFieldRef="{{{field_guid}}}">')
    lines.append(
        f'    <CurvedTrackLine lastModified="1970-01-01T00:00:00" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{curve_guid}}}" '
        f'spatialGeometryType="point" '
        f'fileName="CurveTrack{curve_guid}" '
        f'name="{ascii_safe(track_name)}">'
    )
    lines.append(
        f'      <spatial:MBR uomSource="arcdeg" uomTarget="arcdeg" '
        f'north="{n:.14f}" south="{s:.14f}" '
        f'east="{e:.14f}" west="{w:.14f}" />'
    )
    lines.append(
        '      <spatial:dtSignalType value="dtiSignalTypeUnknown" '
        'definedTypeRepresentation="dtSignalType" />'
    )
    lines.append(
        '      <rcdscbase:vrEastShiftComponent value="0" sourceUOM="m" '
        'variableRepresentation="vrEastShiftComponent" />'
    )
    lines.append(
        '      <rcdscbase:vrNorthShiftComponent value="0" sourceUOM="m" '
        'variableRepresentation="vrNorthShiftComponent" />'
    )
    lines.append(
        f'      <rcdscbase:vrReferenceLatitude value="{ref_lat:.13f}" '
        f'sourceUOM="arcdeg" variableRepresentation="vrLatitude" />'
    )
    lines.append(
        f'      <rcdscbase:vrReferenceLongitude value="{ref_lon:.13f}" '
        f'sourceUOM="arcdeg" variableRepresentation="vrLongitude" />'
    )
    lines.append('    </CurvedTrackLine>')
    lines.append('  </SpatialItems>')
    lines.append('</rcdscfldie:SpatialCatalog>')
    return '\r\n'.join(lines)


def _spatial_catalog_export_only(client_guid, farm_guid, field_guid,
                                  client_name, farm_name, field_name,
                                  now_str, session_uuid, node_uuid):
    lines = []
    lines.append('<?xml version="1.0" encoding="utf-8"?>')
    lines.append(
        '<rcdscfld:SpatialCatalog '
        'xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" '
        'xmlns:bt="urn:schemas-johndeere-com:BasicTypes" '
        'xmlns:rep="urn:schemas-johndeere-com:Representation" '
        'xmlns:unit="urn:schemas-johndeere-com:UnitSystem" '
        'xmlns:rcdscbase="urn:schemas-johndeere-com:RCD:SpatialCatalog:Base" '
        'xmlns:rcdsetup="urn:schemas-johndeere-com:RCD:Setup" '
        'xmlns:rcdscfld="urn:schemas-johndeere-com:RCD:SpatialCatalog:Field">'
    )
    lines.append('  <rcdscfld:FileSchemaVersion nonProductionCode="0">')
    lines.append('    <bt:FileSchemaContentVersion major="1" minor="3" />')
    lines.append('    <bt:UnitOfMeasureVersion major="1" minor="43" />')
    lines.append('    <bt:RepresentationSystemVersion major="4" minor="161" />')
    lines.append('  </rcdscfld:FileSchemaVersion>')
    lines.append(
        f'  <rcdscfld:SourceApp major="3" minor="0" build="0" revision="117" '
        f'nameSourceApp="RCD Target Provider" '
        f'uuidSourceApp="{{b050528e-f328-4dd8-9e9c-71fe8153692e}}" '
        f'uuidSourceAppNode="{{{node_uuid}}}" '
        f'uuidSession="{{{session_uuid}}}" />'
    )
    lines.append(f'  <rcdscfld:SpatialItems eridFieldRef="{{{field_guid}}}" />')
    lines.append('</rcdscfld:SpatialCatalog>')
    return '\r\n'.join(lines)


def _spatial_catalog_water(client_guid, farm_guid, field_guid,
                            client_name, farm_name, field_name,
                            now_str, session_uuid, node_uuid):
    lines = []
    lines.append('<?xml version="1.0" encoding="utf-8"?>')
    lines.append(
        '<rcdscfldiewm:SpatialCatalog '
        'xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" '
        'xmlns:bt="urn:schemas-johndeere-com:BasicTypes" '
        'xmlns:rep="urn:schemas-johndeere-com:Representation" '
        'xmlns:unit="urn:schemas-johndeere-com:UnitSystem" '
        'xmlns:rcdscbase="urn:schemas-johndeere-com:RCD:SpatialCatalog:Base" '
        'xmlns:rcdsetup="urn:schemas-johndeere-com:RCD:Setup" '
        'xmlns:rcdscfldiewm="urn:schemas-johndeere-com:RCD:SpatialCatalog:FieldImportExportWaterManagment">'
    )
    lines.append('  <FileSchemaVersion nonProductionCode="0">')
    lines.append('    <bt:FileSchemaContentVersion major="1" minor="9" />')
    lines.append('    <bt:UnitOfMeasureVersion major="1" minor="43" />')
    lines.append('    <bt:RepresentationSystemVersion major="4" minor="161" />')
    lines.append('  </FileSchemaVersion>')
    lines.append(
        f'  <SourceApp major="3" minor="0" build="0" revision="117" '
        f'nameSourceApp="RCD Target Provider" '
        f'uuidSourceApp="{{b050528e-f328-4dd8-9e9c-71fe8153692e}}" '
        f'uuidSourceAppNode="{{{node_uuid}}}" '
        f'uuidSession="{{{session_uuid}}}" />'
    )
    lines.append('  <Setup>')
    lines.append('    <rcdsetup:FileSchemaVersion nonProductionCode="0">')
    lines.append('      <bt:FileSchemaContentVersion major="3" minor="28" />')
    lines.append('      <bt:UnitOfMeasureVersion major="1" minor="43" />')
    lines.append('      <bt:RepresentationSystemVersion major="4" minor="161" />')
    lines.append('    </rcdsetup:FileSchemaVersion>')
    lines.append('    <bt:Synchronization>')
    lines.append('      <bt:NodeVersions>')
    lines.append(f'        <bt:Node uuid="{{{node_uuid}}}" lastSeen="{now_str}" />')
    lines.append('      </bt:NodeVersions>')
    lines.append('      <bt:EntityDeletions />')
    lines.append('    </bt:Synchronization>')
    lines.append('    <rcdsetup:Participant>')
    lines.append(
        f'      <rcdsetup:Client lastModified="1970-01-01T00:00:00" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{client_guid}}}" '
        f'name="{ascii_safe(client_name)}" />'
    )
    lines.append('    </rcdsetup:Participant>')
    lines.append(
        f'    <rcdsetup:Farm lastModified="{now_str}" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{farm_guid}}}" '
        f'name="{ascii_safe(farm_name)}" clientRef="{{{client_guid}}}" />'
    )
    lines.append(
        f'    <rcdsetup:Field lastModified="{now_str}" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{field_guid}}}" '
        f'name="{ascii_safe(field_name)}" farmRef="{{{farm_guid}}}" />'
    )
    lines.append('    <rcdsetup:Products />')
    lines.append('  </Setup>')
    lines.append(f'  <SpatialItems eridFieldRef="{{{field_guid}}}" />')
    lines.append('</rcdscfldiewm:SpatialCatalog>')
    return '\r\n'.join(lines)


def _setup_fds_minimal(client_guid, farm_guid, field_guid,
                        client_name, farm_name, field_name,
                        now_str, session_uuid, node_uuid):
    """setup.fds mínimo — apenas hierarquia cliente/fazenda/campo, sem frota."""
    lines = []
    lines.append('<?xml version="1.0" encoding="utf-8"?>')
    lines.append(
        '<SetupFile '
        'xmlns:bt="urn:schemas-johndeere-com:BasicTypes" '
        'xmlns:rep="urn:schemas-johndeere-com:Representation" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:spatial="urn:schemas-johndeere-com:SpatialTypes" '
        'xmlns:unit="urn:schemas-johndeere-com:UnitSystem" '
        'xmlns="urn:schemas-johndeere-com:RCD:Setup">'
    )
    lines.append(
        f'  <SourceApp major="3" minor="0" build="0" revision="117" '
        f'nameSourceApp="AgroExport-QGIS" '
        f'uuidSourceApp="{{b050528e-f328-4dd8-9e9c-71fe8153692e}}" '
        f'uuidSourceAppNode="{{{node_uuid}}}" '
        f'uuidSession="{{{session_uuid}}}" />'
    )
    lines.append('  <Setup>')
    lines.append('    <FileSchemaVersion nonProductionCode="0">')
    lines.append('      <bt:FileSchemaContentVersion major="3" minor="27" />')
    lines.append('      <bt:UnitOfMeasureVersion major="1" minor="43" />')
    lines.append('      <bt:RepresentationSystemVersion major="4" minor="161" />')
    lines.append('    </FileSchemaVersion>')
    lines.append('    <bt:Synchronization>')
    lines.append('      <bt:NodeVersions>')
    lines.append(f'        <bt:Node uuid="{{{node_uuid}}}" lastSeen="{now_str}" />')
    lines.append('      </bt:NodeVersions>')
    lines.append('      <bt:EntityDeletions />')
    lines.append('    </bt:Synchronization>')
    lines.append('    <Participant>')
    lines.append(
        f'      <Client lastModified="1970-01-01T00:00:00" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{client_guid}}}" '
        f'name="{ascii_safe(client_name)}" />'
    )
    lines.append('    </Participant>')
    lines.append(
        f'    <Farm lastModified="{now_str}" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{farm_guid}}}" '
        f'name="{ascii_safe(farm_name)}" clientRef="{{{client_guid}}}" />'
    )
    lines.append(
        f'    <Field lastModified="{now_str}" '
        f'sourceNode="{{{node_uuid}}}" erid="{{{field_guid}}}" '
        f'name="{ascii_safe(field_name)}" farmRef="{{{farm_guid}}}" />'
    )
    lines.append('    <Products />')
    lines.append('    <Equipment />')
    lines.append('  </Setup>')
    lines.append('</SetupFile>')
    return '\r\n'.join(lines)


def export_gs3_zip(lines, output_dir, prefix, client_name, farm_name, field_name):
    """
    Gera pacote GS3_2630 .zip com SpatialCatalog correto.
    O terminal regera o CurveTrack*.fdShape ao sincronizar.
    """
    if not lines:
        raise ValueError("Nenhuma feição válida.")

    client_guid  = new_guid()
    farm_guid    = new_guid()
    field_guid   = new_guid()
    curve_guid   = new_guid()
    session_uuid = new_guid()
    node_uuid    = new_guid()
    now_str      = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.0000000Z')

    # Bounding box global
    all_lats = [p[0] for gl in lines for p in gl['pts']]
    all_lons = [p[1] for gl in lines for p in gl['pts']]
    bounds = {
        'north': max(all_lats), 'south': min(all_lats),
        'east':  max(all_lons), 'west':  min(all_lons),
    }

    # Nome da trilha = nome mais comum nos talhões
    track_name = (lines[0]['talhao'] or lines[0]['name'] or 'CURVA').upper()

    c     = ascii_safe(client_name) or 'Cliente'
    # hex2 = primeiros 2 hex do field_guid para a pasta intermediária
    hex2  = field_guid.replace('-', '')[:2].upper()

    fld_dir  = os.path.join(output_dir, 'GS3_2630', c, 'RCD', 'EIC',
                             'Fields', hex2, field_guid)
    eic_dir  = os.path.join(output_dir, 'GS3_2630', c, 'RCD', 'EIC')
    os.makedirs(fld_dir, exist_ok=True)
    os.makedirs(eic_dir, exist_ok=True)

    # setup.fds
    setup_content = _setup_fds_minimal(
        client_guid, farm_guid, field_guid,
        client_name, farm_name, field_name,
        now_str, session_uuid, node_uuid
    )
    with open(os.path.join(eic_dir, 'setup.fds'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(setup_content)

    # global.ver: 0x07 + 0xFF * 1023
    with open(os.path.join(eic_dir, 'global.ver'), 'wb') as f:
        f.write(bytes([0x07]) + bytes([0xFF] * 1023))

    # host: 32 bytes aleatórios
    with open(os.path.join(eic_dir, 'host'), 'wb') as f:
        f.write(uuid.uuid4().bytes + uuid.uuid4().bytes)

    # ImportExport.SpatialCatalog
    ie_content = _spatial_catalog_import_export(
        client_guid, farm_guid, field_guid,
        curve_guid, track_name,
        client_name, farm_name, field_name,
        bounds, now_str, session_uuid, node_uuid
    )
    with open(os.path.join(fld_dir, 'ImportExport.SpatialCatalog'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(ie_content)

    # ExportOnly.SpatialCatalog
    eo_content = _spatial_catalog_export_only(
        client_guid, farm_guid, field_guid,
        client_name, farm_name, field_name,
        now_str, session_uuid, node_uuid
    )
    with open(os.path.join(fld_dir, 'ExportOnly.SpatialCatalog'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(eo_content)

    # WaterManagement.SpatialCatalog
    wm_content = _spatial_catalog_water(
        client_guid, farm_guid, field_guid,
        client_name, farm_name, field_name,
        now_str, session_uuid, node_uuid
    )
    with open(os.path.join(fld_dir, 'WaterManagement.SpatialCatalog'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(wm_content)

    return os.path.join(output_dir, 'GS3_2630')


# ── 4) Formato AgData (PTx Trimble Precision-IQ / GFX-750/1050/1060) ──────────
#
#  Estrutura:
#  AgData/
#  └── Fields/
#      └── {field_uuid}.agf   ← zip contendo manifest.xml + {uuid}.xml.gz.enc
#
#  Criptografia: AES-128-CBC
#  Chave: UUID do campo (sem traços) XOR "e989715d4caa119b5fc8eac3ac46b7c3"
#  IV: gerado aleatoriamente, salvo no manifest.xml
#  Conteúdo: XML com linhas em coordenadas ECEF (metros, float64 XYZ)
#
#  Geometria binária: 1 byte (0x01 LE) + 4 bytes uint32 (npts) + n×24 bytes (XYZ double)
#
#  Compatível com: GFX-750, GFX-1050, GFX-1060, TMX-2050, XCN-1050, XCN-750

_TRIMBLE_XOR = bytes.fromhex("e989715d4caa119b5fc8eac3ac46b7c3")
_WGS84_A  = 6378137.0
_WGS84_E2 = 6.69437999014e-3

def _latlon_to_ecef(lat_deg, lon_deg, h=0.0):
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    N = _WGS84_A / math.sqrt(1 - _WGS84_E2 * math.sin(lat)**2)
    x = (N + h) * math.cos(lat) * math.cos(lon)
    y = (N + h) * math.cos(lat) * math.sin(lon)
    z = (N * (1 - _WGS84_E2) + h) * math.sin(lat)
    return x, y, z

def _encode_agdata_geometry(pts_latlon):
    """Encodes list of (lat, lon) as Trimble ECEF binary, base64."""
    import struct as _s
    n = len(pts_latlon)
    buf = _s.pack('<BI', 1, n)
    for lat, lon in pts_latlon:
        x, y, z = _latlon_to_ecef(lat, lon)
        buf += _s.pack('<3d', x, y, z)
    return base64.b64encode(buf).decode('ascii')

def _make_agf_bytes(lines, field_name, client_name, farm_name, field_uuid):
    """Returns (manifest_bytes, encrypted_bytes, iv_hex, enc_filename)."""
    import gzip as _gz, io as _io, struct as _s
    from .crypto_agdata import aes_cbc_encrypt as _aes_enc, pkcs7_pad as _pkcs7_pad

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

    xml_parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<field uuid="{field_uuid}" isDeleted="false">',
        f'  <name>{ascii_safe(field_name)}</name>',
        f'  <modified>{now}</modified>',
        f'  <time>1970-01-01T00:00:00.000Z</time>',
        f'  <origin></origin>',
        f'  <client>{ascii_safe(client_name)}</client>',
        f'  <farm>{ascii_safe(farm_name)}</farm>',
        f'  <landmarks>',
    ]
    for gl in lines:
        line_uuid = new_guid()
        name = ascii_safe(gl.get('talhao') or gl.get('name') or 'CURVA').upper()
        geom_b64 = _encode_agdata_geometry(gl['pts'])
        xml_parts += [
            f'    <line uuid="{line_uuid}" isDeleted="false">',
            f'      <modified>{now}</modified>',
            f'      <time>1970-01-01T00:00:00.000Z</time>',
            f'      <name>{name}</name>',
            f'      <category>GENERIC</category>',
            f'      <geometry>{geom_b64}</geometry>',
            f'    </line>',
        ]
    xml_parts += ['  </landmarks>', '</field>']
    xml_bytes = '\n'.join(xml_parts).encode('utf-8')

    # Gzip
    buf = _io.BytesIO()
    with _gz.GzipFile(fileobj=buf, mode='wb', mtime=0) as gz:
        gz.write(xml_bytes)
    compressed = buf.getvalue()

    # AES-128-CBC
    key = bytes(a ^ b for a, b in zip(
        bytes.fromhex(field_uuid.replace('-', '')), _TRIMBLE_XOR))
    iv = os.urandom(16)
    padded = _pkcs7_pad(compressed)
    encrypted = _aes_enc(key, iv, padded)

    enc_filename = f'{field_uuid}.xml.gz.enc'
    manifest = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<manifest>'
        f'<name>{ascii_safe(field_name)}</name>'
        f'<uuid>{field_uuid}</uuid>'
        f'<source />'
        f'<key><version>0</version><iv>{iv.hex()}</iv></key>'
        f'<entries><entry>{enc_filename}</entry></entries>'
        f'</manifest>'
    ).encode('utf-8')

    return manifest, encrypted, iv.hex(), enc_filename


def export_agdata_zip(lines, output_dir, prefix, client_name, farm_name, field_name):
    """
    Gera pacote AgData .zip compatível com PTx Trimble Precision-IQ
    (GFX-750, GFX-1050, GFX-1060, TMX-2050).

    Copiar para USB: AgData/Fields/{uuid}.agf
    No monitor: Gerenciar Dados → USB → Campos → Importar
    """
    from .crypto_agdata import _get_backend as _check_crypto
    _check_crypto()  # raises ImportError with helpful message if missing

    if not lines:
        raise ValueError("Nenhuma feição válida.")

    field_uuid = new_guid()
    fields_dir = os.path.join(output_dir, 'AgData', 'Fields')
    os.makedirs(fields_dir, exist_ok=True)

    manifest_bytes, enc_bytes, iv_hex, enc_filename = _make_agf_bytes(
        lines, field_name, client_name, farm_name, field_uuid
    )

    agf_path = os.path.join(fields_dir, f'{field_uuid}.agf')
    with zipfile.ZipFile(agf_path, 'w', zipfile.ZIP_STORED) as zf:
        zf.writestr('manifest.xml', manifest_bytes)
        zf.writestr(enc_filename, enc_bytes)

    return os.path.join(output_dir, 'AgData')


# ── 5) Formato .isg (GS3 / Gen 4 legacy XML) ─────────────────

def export_gs3_isg(lines, path):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        w = lambda s: f.write(s + "\r\n")
        w('<?xml version="1.0" encoding="UTF-8"?>')
        w(f'<GuidanceSetupFile version="3.0" creator="AgroExport-QGIS">')
        w(f'  <CreatedDate>{now}</CreatedDate>')
        w(f'  <TotalLines>{len(lines)}</TotalLines>')
        w('  <GuidanceLines>')
        for i, gl in enumerate(lines, 1):
            tp = "STRAIGHT" if gl["tipo"].upper() == "AB" else "CURVE"
            h  = heading_deg(gl["pts"][0], gl["pts"][-1])
            w(f'    <GuidanceLine id="{i}">')
            w(f'      <Name>{ascii_safe(gl["talhao"] or gl["name"])}</Name>')
            w(f'      <Cliente>{ascii_safe(gl["cliente"])}</Cliente>')
            w(f'      <Fazenda>{ascii_safe(gl["fazenda"])}</Fazenda>')
            w(f'      <Talhao>{ascii_safe(gl["talhao"])}</Talhao>')
            w(f'      <Type>{tp}</Type>')
            w(f'      <Heading>{h:.4f}</Heading>')
            if tp == "STRAIGHT":
                a, b = gl["pts"][0], gl["pts"][-1]
                w(f'      <PointA lat="{a[0]:.8f}" lon="{a[1]:.8f}"/>')
                w(f'      <PointB lat="{b[0]:.8f}" lon="{b[1]:.8f}"/>')
            w(f'      <Waypoints count="{len(gl["pts"])}">')
            for lat, lon in gl["pts"]:
                w(f'        <Point lat="{lat:.8f}" lon="{lon:.8f}"/>')
            w('      </Waypoints>')
            w('    </GuidanceLine>')
        w('  </GuidanceLines>')
        w('</GuidanceSetupFile>')
