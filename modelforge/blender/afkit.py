"""afkit : petite boîte à outils de modélisation, chargée dans Blender.

Toutes ces fonctions sont disponibles directement (sans import) dans le code
que l'agent envoie à Blender. Elles n'empêchent pas d'utiliser bpy/bmesh.

Conventions de la scène :
  - unités : mètres ;
  - X = longueur (l'avant du modèle pointe vers +X) ;
  - Y = largeur (la gauche du modèle est vers +Y) ;
  - Z = hauteur (le sol est à Z = 0).
"""

import math

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector

VIEW_PREFIX = "VUE_"

# En mode « en direct » (live.py), fonction appelée à chaque pièce ajoutée à
# la scène, pour redessiner la fenêtre de Blender au fil de la construction.
_step_hook = None


def _step():
    if _step_hook is not None:
        _step_hook()


# --------------------------------------------------------------------------
# Scène
# --------------------------------------------------------------------------

def clear():
    """Vide entièrement la scène (objets, maillages, matériaux, collections)."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    scene = bpy.context.scene
    scene.frame_set(0)
    if "af_end" in scene:
        del scene["af_end"]
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.cameras,
                  bpy.data.lights, bpy.data.curves, bpy.data.actions):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def _link(obj, collection=None):
    target = _collection(collection) if collection else bpy.context.scene.collection
    target.objects.link(obj)
    _step()
    return obj


def _collection(name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll


def put_in(collection, *objs):
    """Range des objets dans une collection (créée si besoin). Sert à organiser
    le modèle : "Exterieur", "Cabine", "Cockpit"..."""
    coll = _collection(collection)
    for obj in _flatten(objs):
        for old in list(obj.users_collection):
            old.objects.unlink(obj)
        coll.objects.link(obj)
    return coll


def _flatten(items):
    out = []
    for item in items:
        if isinstance(item, (list, tuple, set)):
            out.extend(_flatten(item))
        elif item is not None:
            out.append(item)
    return out


def get(name):
    """Renvoie l'objet portant ce nom, ou None."""
    return bpy.data.objects.get(name)


def delete(*objs):
    """Supprime des objets (objets ou noms)."""
    for obj in _flatten(objs):
        if isinstance(obj, str):
            obj = bpy.data.objects.get(obj)
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)


# --------------------------------------------------------------------------
# Matériaux
# --------------------------------------------------------------------------

def _rgba(color, alpha=1.0):
    if isinstance(color, str):
        h = color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        srgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        # hex = sRGB (ce qu'on voit à l'écran) -> linéaire (ce que Blender attend)
        lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb]
        return (*lin, alpha)
    color = tuple(color)
    return (color[0], color[1], color[2], alpha)


# Finitions prêtes à l'emploi : réglages du matériau (métal, rugosité, vernis,
# transparence...) et, pour certaines, un léger relief visible sur la photo réaliste.
FINISHES = {
    "mat": {"roughness": 0.9},
    "satine": {"roughness": 0.45},
    "brillant": {"roughness": 0.12, "coat": 0.4},
    "plastique": {"roughness": 0.4},
    "caoutchouc": {"roughness": 0.95},
    "peinture_auto": {"metallic": 0.35, "roughness": 0.35, "coat": 1.0},
    "ceramique": {"roughness": 0.15, "coat": 0.6},
    "metal": {"metallic": 1.0, "roughness": 0.35},
    "metal_brosse": {"metallic": 1.0, "roughness": 0.4, "relief": ("fibres", 0.15)},
    "inox": {"metallic": 1.0, "roughness": 0.25},
    "chrome": {"metallic": 1.0, "roughness": 0.04},
    "or": {"metallic": 1.0, "roughness": 0.2},
    "verre": {"alpha": 0.22, "roughness": 0.03},
    "verre_teinte": {"alpha": 0.5, "roughness": 0.03},
    "bois": {"roughness": 0.6, "relief": ("fibres", 0.5)},
    "beton": {"roughness": 0.92, "relief": ("grains", 0.6)},
    "tissu": {"roughness": 1.0, "sheen": 0.6, "relief": ("grains", 0.25)},
    "cuir": {"roughness": 0.55, "relief": ("grains", 0.35)},
    "lumiere": {"roughness": 0.5, "glow": 4.0},
    "flamme": {"roughness": 0.5, "glow": 6.0, "alpha": 0.55},
    "fumee": {"roughness": 1.0, "alpha": 0.45},
}


def _relief(material, bsdf, kind, strength):
    """Petit relief procédural (fibres étirées ou grains). Il ne se voit que
    sur le rendu réaliste ; la visionneuse garde la surface lisse."""
    nodes, links = material.node_tree.nodes, material.node_tree.links
    for node in [n for n in nodes if n.name.startswith("af_")]:
        nodes.remove(node)
    if not kind:
        return
    coords = nodes.new("ShaderNodeTexCoord")
    mapping = nodes.new("ShaderNodeMapping")
    noise = nodes.new("ShaderNodeTexNoise")
    bump = nodes.new("ShaderNodeBump")
    for node, tag in ((coords, "coords"), (mapping, "mapping"), (noise, "noise"), (bump, "bump")):
        node.name = "af_" + tag
    mapping.inputs["Scale"].default_value = (1.0, 14.0, 14.0) if kind == "fibres" else (1.0, 1.0, 1.0)
    noise.inputs["Scale"].default_value = 18.0 if kind == "fibres" else 90.0
    noise.inputs["Detail"].default_value = 6.0
    bump.inputs["Strength"].default_value = strength
    bump.inputs["Distance"].default_value = 0.01
    links.new(coords.outputs["Object"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    links.new(noise.outputs[0], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])


def mat(name, color="#cccccc", metallic=None, roughness=None, alpha=None,
        emission=None, emission_strength=1.0, finish=None):
    """Crée (ou met à jour) un matériau et le renvoie.

    color : "#rrggbb" (couleur écran) ou (r, g, b) linéaire entre 0 et 1.
    finish : finition prête à l'emploi, qui règle l'aspect de la surface :
        "mat", "satine", "brillant", "plastique", "caoutchouc",
        "peinture_auto" (carrosserie vernie), "ceramique",
        "metal", "metal_brosse", "inox", "chrome", "or",
        "verre", "verre_teinte" (transparents),
        "bois", "beton", "tissu", "cuir" (avec un léger relief sur la photo),
        "lumiere" (la couleur est émise : phares, écrans),
        "flamme" (lumineuse et translucide), "fumee" (translucide et mate).
      Préfère une finition aux réglages à la main : le rendu est plus juste.
    metallic, roughness, alpha : réglages fins, qui priment sur la finition.
    alpha < 1 : matériau transparent.
    emission : couleur émise, même format que color.
    """
    if finish is not None and finish not in FINISHES:
        raise ValueError(f"mat : finition inconnue « {finish} ». Disponibles : {', '.join(FINISHES)}")
    preset = FINISHES.get(finish, {})
    metallic = preset.get("metallic", 0.0) if metallic is None else metallic
    roughness = preset.get("roughness", 0.5) if roughness is None else roughness
    alpha = preset.get("alpha", 1.0) if alpha is None else alpha
    if emission is None and "glow" in preset:
        emission, emission_strength = color, preset["glow"]
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    try:
        material.use_nodes = True
    except Exception:
        pass
    rgba = _rgba(color, alpha)
    material.diffuse_color = rgba
    material.metallic = min(metallic, 0.3)  # affichage simplifié : sinon presque noir
    material.roughness = roughness
    bsdf = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (*rgba[:3], 1.0)
        bsdf.inputs["Metallic"].default_value = metallic
        bsdf.inputs["Roughness"].default_value = roughness
        bsdf.inputs["Alpha"].default_value = alpha
        for socket, key in (("Coat Weight", "coat"), ("Sheen Weight", "sheen")):
            try:
                bsdf.inputs[socket].default_value = preset.get(key, 0.0)
            except Exception:
                pass
        if emission is not None:
            bsdf.inputs["Emission Color"].default_value = (*_rgba(emission)[:3], 1.0)
            bsdf.inputs["Emission Strength"].default_value = emission_strength
        try:
            _relief(material, bsdf, *preset.get("relief", (None, 0.0)))
        except Exception:
            pass
    if alpha < 1.0:
        for attr, value in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
            try:
                setattr(material, attr, value)
            except Exception:
                pass
    return material


GRADIENT = "af_degrade"


def noise(x, y, z, scale=1.0):
    """Bruit lisse entre 0 et 1 au point (x, y, z) : valeurs voisines pour des
    points voisins. scale grand = motif fin. Sert dans paint() à faire des
    bords irréguliers et naturels (coulées, taches, nuages)."""
    from mathutils import noise as _noise
    value = _noise.fractal(Vector((x, y, z)) * scale, 1.0, 2.0, 4)
    return min(max(0.5 + 0.5 * value, 0.0), 1.0)


def _base_color(obj):
    for material in obj.data.materials:
        if material is None:
            continue
        if "af_base" in material:
            return tuple(material["af_base"])
        bsdf = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is not None:
            return tuple(bsdf.inputs["Base Color"].default_value)[:3]
    return (0.8, 0.8, 0.8)


def _vertex_paint(obj, color_of):
    """color_of(point monde) -> (r, g, b, a) linéaire. Écrit la couleur de
    chaque sommet et branche les matériaux de l'objet dessus."""
    bpy.context.view_layer.update()
    mesh = obj.data
    base = _base_color(obj)
    layer = mesh.color_attributes.get(GRADIENT) or mesh.color_attributes.new(GRADIENT, "FLOAT_COLOR", "POINT")
    translucent = False
    for vertex in mesh.vertices:
        rgba = color_of(obj.matrix_world @ vertex.co, base)
        translucent = translucent or rgba[3] < 0.999
        layer.data[vertex.index].color = rgba
    mesh.color_attributes.active_color = layer
    try:
        mesh.color_attributes.render_color_index = list(mesh.color_attributes).index(layer)
    except Exception:
        pass

    for slot, material in enumerate(mesh.materials):
        if material is None:
            continue
        if not material.get("af_gradient"):
            # Copie du matériau propre à cet objet : les autres objets qui
            # partagent l'original gardent leur couleur unie.
            copy = material.copy()
            copy.name = material.name + " peint"
            copy["af_gradient"] = True
            copy["af_base"] = base
            mesh.materials[slot] = material = copy
        nodes, links = material.node_tree.nodes, material.node_tree.links
        bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            continue
        attribute = nodes.get("af_couleur") or nodes.new("ShaderNodeVertexColor")
        attribute.name = "af_couleur"
        attribute.layer_name = GRADIENT
        bsdf.inputs["Base Color"].default_value = (1, 1, 1, 1)
        links.new(attribute.outputs["Color"], bsdf.inputs["Base Color"])
        if translucent:
            links.new(attribute.outputs["Alpha"], bsdf.inputs["Alpha"])
            for attr, value in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
                try:
                    setattr(material, attr, value)
                except Exception:
                    pass
    return obj


def _as_rgba(value, base):
    """Couleur donnée par l'agent -> (r, g, b, a). None = couleur du matériau."""
    if value is None:
        return (*base, 1.0)
    if isinstance(value, tuple) and len(value) == 2 and not isinstance(value[0], (int, float)):
        color, alpha = value
        return (*(base if color is None else _rgba(color)[:3]), float(alpha))
    return (*_rgba(value)[:3], 1.0)


def paint(obj, color_at):
    """Peint un objet point par point, avec des transitions douces : c'est
    l'outil pour tout motif irrégulier (coulées de givre, suie, rouille,
    taches, camouflage, nuage plus dense au centre). N'affecte JAMAIS des
    matériaux différents face par face pour cela : le résultat est un damier.

    color_at(x, y, z) reçoit la position monde d'un sommet et renvoie :
      None                    -> la couleur normale du matériau ;
      "#rrggbb"               -> cette couleur ;
      ("#rrggbb", opacité)    -> cette couleur, plus ou moins transparente ;
      (None, opacité)         -> la couleur du matériau, plus ou moins transparente.
    Les valeurs sont lissées entre sommets voisins : l'objet doit être finement
    découpé (cylinder(..., segments=96, cuts=120), sphere(segments=64, rings=32),
    subdivide). Utilise noise() pour des bords naturels. Exemple, givre en
    coulées sur une coque fine posée juste au-dessus d'un réservoir :
        def givre(x, y, z):
            coulee = noise(x * 40, y * 40, z * 3)          # étiré en hauteur
            force = max(0.0, coulee - 0.45) * 3.0           # 0 = rien
            return ("#f4f8fb", min(force, 0.95))
        paint(coque_givre, givre)
    Les finitions du matériau (métal, rugosité, lumière) sont conservées."""
    return _vertex_paint(obj, lambda point, base: _as_rgba(color_at(point.x, point.y, point.z), base))


def gradient(obj, stops, axis="Z"):
    """Dégradé de couleur régulier le long d'un axe ("X", "Y" ou "Z", en
    coordonnées monde) : flamme qui pâlit et disparaît, fumée qui se dissipe,
    salissures en bas d'une carrosserie. Pour un motif irrégulier, voir paint.

    stops : liste de (position, couleur) ou (position, couleur, opacité),
    position entre 0 (bas de l'objet sur cet axe) et 1 (haut), couleur au
    format de mat(). Exemple, flamme sous une tuyère (du haut vers le bas) :
        gradient(flamme, [(0, "#ff5a00", 0.0), (0.6, "#ff9a30", 0.5), (1, "#fff2c0", 0.9)])
    Il faut assez de découpes le long de l'axe (cylinder(..., cuts=24),
    sphere, tube, ou subdivide) pour qu'il soit lisse."""
    index = "XYZ".index(axis.upper())
    stops = sorted((s[0], _rgba(s[1]), s[2] if len(s) > 2 else 1.0) for s in stops)
    bpy.context.view_layer.update()
    values = [(obj.matrix_world @ v.co)[index] for v in obj.data.vertices]
    lo = min(values)
    span = max(max(values) - lo, 1e-9)

    def color_of(point, base):
        t = (point[index] - lo) / span
        if t <= stops[0][0]:
            return (*stops[0][1][:3], stops[0][2])
        for (t0, c0, a0), (t1, c1, a1) in zip(stops, stops[1:]):
            if t <= t1:
                k = (t - t0) / max(t1 - t0, 1e-9)
                return (*[c0[i] + (c1[i] - c0[i]) * k for i in range(3)], a0 + (a1 - a0) * k)
        return (*stops[-1][1][:3], stops[-1][2])

    return _vertex_paint(obj, color_of)


def assign(obj, material):
    """Applique un matériau unique à un objet (remplace les précédents)."""
    if isinstance(material, str):
        material = bpy.data.materials.get(material) or mat(material)
    obj.data.materials.clear()
    obj.data.materials.append(material)
    return obj


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def _new_object(name, bm, material=None, collection=None, smooth=False):
    mesh = bpy.data.meshes.new(name)
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    if smooth:
        mesh.shade_smooth()
    obj = bpy.data.objects.new(name, mesh)
    _link(obj, collection)
    if material is not None:
        assign(obj, material)
    return obj


def _place(obj, location, rotation):
    obj.location = Vector(location)
    if rotation is not None:
        obj.rotation_euler = Euler([math.radians(a) for a in rotation])
    return obj


def box(name, size=(1, 1, 1), location=(0, 0, 0), rotation=None, material=None,
        collection=None):
    """Pavé centré sur `location`. size = (longueur X, largeur Y, hauteur Z).
    rotation = (rx, ry, rz) en DEGRÉS."""
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=Vector(size), verts=bm.verts)
    return _place(_new_object(name, bm, material, collection), location, rotation)


_AXIS_ROT = {
    "Z": Matrix.Identity(4),
    "X": Matrix.Rotation(math.radians(90), 4, "Y"),
    "Y": Matrix.Rotation(math.radians(-90), 4, "X"),
}


def cylinder(name, radius=0.5, depth=1.0, location=(0, 0, 0), axis="Z", radius2=None,
             segments=32, rotation=None, material=None, collection=None, smooth=True,
             cuts=0):
    """Cylindre (ou cône tronqué si radius2 est donné) centré sur `location`,
    allongé selon `axis` ("X", "Y" ou "Z"). radius = rayon côté négatif de
    l'axe, radius2 = rayon côté positif. cuts = nombre de découpes le long de
    l'axe (utile avant un dégradé avec gradient)."""
    r2 = radius if radius2 is None else radius2
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=segments,
                          radius1=radius, radius2=r2, depth=depth)
    for k in range(1, int(cuts) + 1):
        z = -depth / 2 + depth * k / (int(cuts) + 1)
        bmesh.ops.bisect_plane(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:],
                               plane_co=(0, 0, z), plane_no=(0, 0, 1))
    bmesh.ops.transform(bm, matrix=_AXIS_ROT[axis.upper()], verts=bm.verts)
    obj = _place(_new_object(name, bm, material, collection, smooth=smooth), location, rotation)
    if smooth:
        obj.data.set_sharp_from_angle(angle=math.radians(40))
    return obj


def sphere(name, radius=0.5, location=(0, 0, 0), scale=(1, 1, 1), segments=32, rings=16,
           material=None, collection=None):
    """Sphère ; `scale` l'étire en ellipsoïde."""
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=rings, radius=radius)
    bmesh.ops.scale(bm, vec=Vector(scale), verts=bm.verts)
    return _place(_new_object(name, bm, material, collection, smooth=True), location, None)


def mesh_from(name, verts, faces, material=None, collection=None, smooth=False):
    """Maillage libre : verts = [(x, y, z), ...], faces = [(i, j, k, ...), ...]."""
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(v) for v in verts], [], [tuple(f) for f in faces])
    mesh.update()
    if smooth:
        mesh.shade_smooth()
    obj = bpy.data.objects.new(name, mesh)
    _link(obj, collection)
    if material is not None:
        assign(obj, material)
    return obj


# --------------------------------------------------------------------------
# Formes par sections (fuselages, coques, ailes, membres...)
# --------------------------------------------------------------------------

def ellipse_ring(x, half_width, half_height, center_y=0.0, center_z=0.0, n=32,
                 flat_bottom=None):
    """Anneau de n points dans le plan perpendiculaire à X, à l'abscisse x.
    half_width = demi-largeur (Y), half_height = demi-hauteur (Z).
    flat_bottom : si donné (altitude Z), les points plus bas sont remontés à
    cette altitude (fond plat)."""
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        y = center_y + half_width * math.cos(a)
        z = center_z + half_height * math.sin(a)
        if flat_bottom is not None and z < flat_bottom:
            z = flat_bottom
        pts.append((x, y, z))
    return pts


def loft(name, rings, cap_start=True, cap_end=True, material=None, collection=None,
         smooth=True):
    """Relie des anneaux successifs par une peau. `rings` est une liste
    d'anneaux ; chaque anneau est une liste de points (x, y, z) ; tous les
    anneaux doivent avoir le même nombre de points, listés dans le même sens.
    C'est l'outil de base pour un fuselage, une coque, un corps profilé."""
    n = len(rings[0])
    if any(len(r) != n for r in rings):
        raise ValueError("loft : tous les anneaux doivent avoir le même nombre de points")
    verts = [p for ring in rings for p in ring]
    faces = []
    for s in range(len(rings) - 1):
        a, b = s * n, (s + 1) * n
        for i in range(n):
            j = (i + 1) % n
            faces.append((a + i, a + j, b + j, b + i))
    if cap_start:
        faces.append(tuple(range(n)))
    if cap_end:
        base = (len(rings) - 1) * n
        faces.append(tuple(base + i for i in reversed(range(n))))
    obj = mesh_from(name, verts, faces, material, collection, smooth=False)
    _fix_normals(obj)
    if smooth:
        obj.data.shade_smooth()
        obj.data.set_sharp_from_angle(angle=math.radians(50))
    return obj


def _fix_normals(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()


def _monotone(xs, ys, x):
    """Interpolation cubique monotone (pas de bosse parasite entre deux
    stations) de la courbe passant par les points (xs, ys), évaluée en x."""
    n = len(xs)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    m = [d[0]] + [0.0 if d[i - 1] * d[i] <= 0 else
                  2 * d[i - 1] * d[i] / (d[i - 1] + d[i]) for i in range(1, n - 1)] + [d[-1]]
    i = max(0, min(n - 2, next((k for k in range(n - 1) if x <= xs[k + 1]), n - 2)))
    t = (x - xs[i]) / h[i]
    return ((2 * t ** 3 - 3 * t ** 2 + 1) * ys[i] + (t ** 3 - 2 * t ** 2 + t) * h[i] * m[i]
            + (-2 * t ** 3 + 3 * t ** 2) * ys[i + 1] + (t ** 3 - t ** 2) * h[i] * m[i + 1])


def tube(name, stations, n=32, material=None, collection=None, cap_start=True,
         cap_end=True, smooth_profile=True, steps=6):
    """Corps profilé allongé selon X (fuselage, nacelle, coque, bouteille...).
    stations = [(x, half_width, half_height), ...] ou
               [(x, half_width, half_height, center_z), ...]   (x croissants)
    smooth_profile=True : le profil est arrondi entre les stations (quelques
    stations suffisent pour un nez et une queue bien galbés) ; False : segments
    droits. Une demi-largeur proche de 0 fait une pointe."""
    stations = sorted(([*st, 0.0][:4] for st in stations), key=lambda st: st[0])
    xs = [st[0] for st in stations]
    if smooth_profile and len(stations) > 2:
        samples = []
        for i in range(len(xs) - 1):
            for k in range(steps):
                samples.append(xs[i] + (xs[i + 1] - xs[i]) * k / steps)
        samples.append(xs[-1])
        cols = [[st[c] for st in stations] for c in (1, 2, 3)]
        stations = [(x, *(_monotone(xs, col, x) for col in cols)) for x in samples]
    rings = []
    for x, hw, hh, cz in stations:
        rings.append(ellipse_ring(x, max(hw, 1e-4), max(hh, 1e-4), 0.0, cz, n))
    return loft(name, rings, cap_start, cap_end, material, collection)


def _airfoil(chord, thickness, n):
    """Profil symétrique type NACA : liste de (distance depuis le bord
    d'attaque, demi-épaisseur signée), bord de fuite -> extrados -> bord
    d'attaque -> intrados."""
    pts = []
    half = n // 2
    for i in range(n):
        if i <= half:
            t = 1 - i / half           # 1 -> 0 (extrados)
            sign = 1
        else:
            t = (i - half) / (n - half)  # 0 -> 1 (intrados)
            sign = -1
        xc = 0.5 * (1 - math.cos(math.pi * t))  # resserre les points aux bords
        yt = 5 * thickness * (0.2969 * math.sqrt(xc) - 0.1260 * xc - 0.3516 * xc ** 2
                              + 0.2843 * xc ** 3 - 0.1036 * xc ** 4)
        pts.append((xc * chord, sign * yt * chord))
    return pts


def wing(name, stations, vertical=False, n=24, material=None, collection=None):
    """Surface portante (aile, dérive, empennage, pale, aileron de voiture).

    stations = liste de dicts, de l'emplanture vers le saumon :
        {"le": (x, y, z), "chord": c, "thickness": 0.12, "twist": 0}
      le        : position du BORD D'ATTAQUE (le point le plus en avant, +X)
      chord     : corde = longueur du profil, du bord d'attaque vers -X
      thickness : épaisseur relative (0.12 = 12 % de la corde)
      twist     : vrillage en degrés (optionnel)
    vertical=False : aile à plat (épaisseur selon Z, envergure selon Y).
    vertical=True  : dérive (épaisseur selon Y, envergure selon Z).
    Pour l'aile opposée : mirror(obj)."""
    rings = []
    for st in stations:
        le = Vector(st["le"])
        twist = math.radians(st.get("twist", 0.0))
        ring = []
        for d, t in _airfoil(st["chord"], st.get("thickness", 0.12), n):
            dx = -d * math.cos(twist) + t * math.sin(twist)
            dt = d * math.sin(twist) + t * math.cos(twist)
            if vertical:
                ring.append((le.x + dx, le.y + dt, le.z))
            else:
                ring.append((le.x + dx, le.y, le.z + dt))
        rings.append(ring)
    return loft(name, rings, True, True, material, collection)


# --------------------------------------------------------------------------
# Modificateurs et opérations
# --------------------------------------------------------------------------

def _apply_modifier(obj, modifier):
    with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj]):
        bpy.ops.object.modifier_apply(modifier=modifier.name)


def mirror(obj, axis="Y"):
    """Symétrise l'objet par rapport au plan passant par l'ORIGINE DU MONDE
    (axis="Y" : symétrie gauche/droite). L'objet garde son nom et contient les
    deux moitiés."""
    empty = bpy.data.objects.new("_afkit_origin", None)
    bpy.context.scene.collection.objects.link(empty)
    bpy.context.view_layer.update()
    mod = obj.modifiers.new("Miroir", "MIRROR")
    mod.use_axis = [a == axis.upper() for a in "XYZ"]
    mod.mirror_object = empty
    mod.use_mirror_merge = False
    _apply_modifier(obj, mod)
    bpy.data.objects.remove(empty, do_unlink=True)
    return obj


def shell(obj, thickness=0.1, inner_material=None):
    """Donne une épaisseur à une peau (vers l'intérieur) : le volume devient
    creux et visitable. inner_material : matériau de la face intérieure
    (habillage de cabine, murs intérieurs)."""
    if inner_material is not None:
        if isinstance(inner_material, str):
            inner_material = bpy.data.materials.get(inner_material) or mat(inner_material)
        if not obj.data.materials:
            obj.data.materials.append(mat("Defaut"))
        obj.data.materials.append(inner_material)
    mod = obj.modifiers.new("Epaisseur", "SOLIDIFY")
    mod.thickness = thickness
    mod.offset = -1.0
    if inner_material is not None:
        mod.material_offset = len(obj.data.materials) - 1
    _apply_modifier(obj, mod)
    return obj


def cut(obj, cutter, keep_cutter=False):
    """Retire de `obj` le volume de `cutter` (ouverture de porte, fenêtre,
    évidement). `cutter` est supprimé ensuite, sauf keep_cutter=True."""
    mod = obj.modifiers.new("Decoupe", "BOOLEAN")
    mod.operation = "DIFFERENCE"
    mod.object = cutter
    mod.solver = "EXACT"
    _apply_modifier(obj, mod)
    if not keep_cutter:
        delete(cutter)
    return obj


def bevel(obj, width=0.02, segments=2):
    """Arrondit les arêtes vives."""
    mod = obj.modifiers.new("Chanfrein", "BEVEL")
    mod.width = width
    mod.segments = segments
    mod.limit_method = "ANGLE"
    _apply_modifier(obj, mod)
    obj.data.shade_smooth()
    obj.data.set_sharp_from_angle(angle=math.radians(40))
    return obj


def subdivide(obj, levels=1):
    """Lisse la forme par subdivision (formes organiques : personnages...)."""
    mod = obj.modifiers.new("Subdivision", "SUBSURF")
    mod.levels = mod.render_levels = levels
    _apply_modifier(obj, mod)
    obj.data.shade_smooth()
    return obj


def join(objs, name=None):
    """Fusionne plusieurs objets en un seul (le premier de la liste)."""
    objs = _flatten(objs)
    target = objs[0]
    with bpy.context.temp_override(object=target, active_object=target,
                                   selected_objects=objs, selected_editable_objects=objs):
        bpy.ops.object.join()
    if name:
        target.name = name
    return target


def copies(obj, placements, prefix=None, collection=None):
    """Répète un objet sans dupliquer sa géométrie (sièges, hublots, roues,
    fenêtres, arbres...). Très léger même pour des centaines d'exemplaires.

    placements = [(x, y, z), ...] ou [((x, y, z), (rx, ry, rz)), ...] (degrés).
    L'objet d'origine reste où il est : c'est un exemplaire comme les autres.
    Renvoie la liste des nouveaux objets."""
    out = []
    prefix = prefix or obj.name
    for i, placement in enumerate(placements, start=1):
        if len(placement) == 2 and hasattr(placement[0], "__len__"):
            location, rotation = placement
        else:
            location, rotation = placement, None
        dup = bpy.data.objects.new(f"{prefix}.{i:03d}", obj.data)
        dup.scale = obj.scale
        dup.rotation_euler = obj.rotation_euler
        _place(dup, location, rotation)
        target = _collection(collection) if collection else (
            obj.users_collection[0] if obj.users_collection else bpy.context.scene.collection)
        target.objects.link(dup)
        out.append(dup)
        _step()
    return out


# --------------------------------------------------------------------------
# Mesures et mise à l'échelle
# --------------------------------------------------------------------------

def _model_objects():
    return [o for o in bpy.context.scene.objects if o.type in {"MESH", "CURVE", "FONT", "SURFACE"}]


def bounds(objs=None):
    """Boîte englobante (min, max) en coordonnées monde, de tout le modèle ou
    des objets donnés. Renvoie (Vector min, Vector max)."""
    objs = _flatten([objs]) if objs else _model_objects()
    bpy.context.view_layer.update()
    lo = Vector((math.inf,) * 3)
    hi = Vector((-math.inf,) * 3)
    for obj in objs:
        for corner in obj.bound_box:
            p = obj.matrix_world @ Vector(corner)
            lo = Vector(map(min, lo, p))
            hi = Vector(map(max, hi, p))
    if lo.x == math.inf:
        return Vector((0, 0, 0)), Vector((0, 0, 0))
    return lo, hi


def dimensions(objs=None):
    """(longueur X, largeur Y, hauteur Z) du modèle ou des objets donnés."""
    lo, hi = bounds(objs)
    return tuple(hi - lo)


def fit_to(length=None, width=None, height=None, ground=True):
    """Met TOUT le modèle à l'échelle, proportions conservées, pour que sa
    longueur (X), sa largeur (Y) ou sa hauteur (Z) vaille la valeur demandée
    en mètres. Une seule des trois valeurs. ground=True repose ensuite le
    modèle sur le sol (Z = 0). Renvoie le facteur appliqué."""
    given = [(i, v) for i, v in enumerate((length, width, height)) if v]
    if len(given) != 1:
        raise ValueError("fit_to : donner exactement une valeur parmi length, width, height")
    axis, target = given[0]
    current = dimensions()[axis]
    if current <= 0:
        raise ValueError("fit_to : la scène est vide")
    factor = target / current
    scale = Matrix.Diagonal((factor, factor, factor, 1.0))
    roots = [o for o in bpy.context.scene.objects if o.parent is None]
    for obj in roots:
        obj.matrix_world = scale @ obj.matrix_world
    if ground:
        drop_to_ground()
    return factor


def drop_to_ground():
    """Translate tout le modèle pour que son point le plus bas soit à Z = 0."""
    lo, _ = bounds()
    shift = Matrix.Translation((0, 0, -lo.z))
    for obj in [o for o in bpy.context.scene.objects if o.parent is None]:
        obj.matrix_world = shift @ obj.matrix_world
    bpy.context.view_layer.update()


# --------------------------------------------------------------------------
# Animation
# --------------------------------------------------------------------------

FPS = 24


def set_origin(obj, point):
    """Déplace le pivot d'un objet au point donné (coordonnées monde) sans
    bouger sa géométrie : c'est autour de ce point qu'il tournera (charnière
    d'une porte, axe d'une roue, articulation d'un bras). À faire avant
    `copies`, car les copies partagent la même géométrie."""
    bpy.context.view_layer.update()  # la position de l'objet doit être à jour
    world = obj.matrix_world.copy()
    local = world.inverted() @ Vector(point)
    obj.data.transform(Matrix.Translation(-local))
    obj.matrix_world = world @ Matrix.Translation(local)
    bpy.context.view_layer.update()
    return obj


def animate(objs, at, location=None, move=None, rotation=None, turn=None, scale=None):
    """Pose une clé d'animation à l'instant `at` (en secondes) pour un objet
    ou une liste d'objets. L'utilisateur joue l'animation dans la visionneuse.

    location : position absolue (x, y, z) à cet instant.
    move     : déplacement (dx, dy, dz) par rapport à la clé précédente ;
               pratique pour déplacer tout un groupe d'un bloc.
    rotation : angles absolus (rx, ry, rz) en DEGRÉS, autour du pivot.
    turn     : angles ajoutés à ceux de la clé précédente, en DEGRÉS.
    scale    : échelle absolue (nombre ou triplet).
    Sans aucun de ces arguments : fige l'état courant à cet instant (pause).

    La pose de départ est enregistrée automatiquement à 0 s à la première
    clé d'un objet. Entre deux clés, le mouvement démarre et finit en douceur.
    À appeler en fin de script, après fit_to."""
    scene = bpy.context.scene
    frame = int(round(at * FPS))
    paths = ("location", "rotation_euler", "scale")
    for obj in _flatten([objs]):
        obj.rotation_mode = "XYZ"
        if obj.animation_data is None or obj.animation_data.action is None:
            for path in paths:
                obj.keyframe_insert(path, frame=0)
        if location is not None:
            obj.location = Vector(location)
        if move is not None:
            obj.location = obj.location + Vector(move)
        if rotation is not None:
            obj.rotation_euler = [math.radians(a) for a in rotation]
        if turn is not None:
            obj.rotation_euler = [r + math.radians(a) for r, a in zip(obj.rotation_euler, turn)]
        if scale is not None:
            obj.scale = (scale,) * 3 if isinstance(scale, (int, float)) else Vector(scale)
        for path in paths:
            obj.keyframe_insert(path, frame=frame)
    scene.frame_start = 0
    scene["af_end"] = max(int(scene.get("af_end", 0)), frame)
    scene.frame_end = max(scene["af_end"], 1)
    scene.render.fps = FPS


def animation_length():
    """Durée de l'animation en secondes (0 s'il n'y en a pas)."""
    scene = bpy.context.scene
    last = int(scene.get("af_end", 0))
    for obj in scene.objects:   # clés posées sans passer par animate()
        action = obj.animation_data.action if obj.animation_data else None
        if action is not None:
            last = max(last, int(action.frame_range[1]))
    return last / max(scene.render.fps, 1)


# --------------------------------------------------------------------------
# Points de vue
# --------------------------------------------------------------------------

def viewpoint(name, location, look_at, fov=75):
    """Enregistre un point de vue nommé (ex. "Cabine", "Cockpit", "Salon").
    Il apparaît comme bouton dans la visionneuse : l'utilisateur s'y
    téléporte d'un clic. location = position de l'œil, look_at = point
    regardé, fov = champ de vision en degrés."""
    full = VIEW_PREFIX + name
    old = bpy.data.objects.get(full)
    if old is not None:
        bpy.data.objects.remove(old, do_unlink=True)
    cam = bpy.data.cameras.new(full)
    cam.sensor_fit = "HORIZONTAL"
    cam.angle = math.radians(fov)
    cam.clip_start = 0.05
    cam.clip_end = 5000
    obj = bpy.data.objects.new(full, cam)
    _collection("Points de vue").objects.link(obj)
    aim(obj, location, look_at)
    return obj


def aim(obj, location, look_at):
    """Place un objet (caméra, lumière) et l'oriente vers un point."""
    obj.location = Vector(location)
    direction = Vector(look_at) - Vector(location)
    if direction.length < 1e-9:
        direction = Vector((1, 0, 0))
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return obj


__all__ = [
    "set_origin", "animate", "gradient", "paint", "noise",
    "clear", "put_in", "get", "delete", "mat", "assign", "box", "cylinder", "sphere",
    "mesh_from", "ellipse_ring", "loft", "tube", "wing", "mirror", "shell", "cut", "bevel",
    "subdivide", "join", "copies", "bounds", "dimensions", "fit_to", "drop_to_ground",
    "viewpoint", "aim",
]
