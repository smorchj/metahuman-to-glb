// Shared three.js viewer for the MetaHuman → GLB gallery.
//
// mount(container, { glbUrl, mappingUrl?, autoRotate?, interactive?, background? })
//   - Loads the GLB (Draco-compressed)
//   - Optionally fetches mh_materials.json and patches materials per MH kind.
//   - MH material reconstructions (kept in sync with stage-02 Blender shaders):
//       cloth  — 2-tone mask blend + detail + macro overlay + AO multiply
//       hair   — synthesized melanin color, atlas R/A channel as alpha mask
//       eye_refractive — iris atlas sampling around mesh pole UV + limbus
//                        darkening + radial iris/sclera + pupil ramp + veins
//       face_accessory — flat defaults (teeth/saliva/eyelashes/occlusion/cartilage)
//       skin   — passthrough (glTF carries the textures already)

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const DRACO_DECODER = 'https://www.gstatic.com/draco/versioned/decoders/1.5.7/';

export async function mount(container, opts) {
  const {
    glbUrl,
    mappingUrl = null,
    autoRotate = false,
    interactive = true,
    background = 0x0b0d11,
  } = opts;

  const w = container.clientWidth || 640;
  const h = container.clientHeight || 480;

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(w, h, false);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.0;
  container.appendChild(renderer.domElement);
  renderer.domElement.style.display = 'block';
  renderer.domElement.style.width = '100%';
  renderer.domElement.style.height = '100%';

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(background);

  const camera = new THREE.PerspectiveCamera(30, w / h, 0.01, 2000);
  camera.position.set(0, 0, 3);

  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.autoRotate = !!autoRotate;
  controls.autoRotateSpeed = 0.6;
  controls.enableZoom = interactive;
  controls.enableRotate = interactive;
  controls.enablePan = false;

  const draco = new DRACOLoader();
  draco.setDecoderPath(DRACO_DECODER);
  const loader = new GLTFLoader();
  loader.setDRACOLoader(draco);

  const [gltf, mapping] = await Promise.all([
    loader.loadAsync(glbUrl),
    mappingUrl ? fetch(mappingUrl).then((r) => (r.ok ? r.json() : null)).catch(() => null)
               : Promise.resolve(null),
  ]);

  scene.add(gltf.scene);

  // Per-character bone pose fixups — MH garments with simulated elements
  // (drawstring laces, loose straps) ship in their authored rest pose, which
  // for Chaos-Cloth-driven bones is usually horizontal. We pose these bones
  // to a hand-tuned settled rotation at load so they hang naturally without
  // needing to re-bake the GLB.
  applyBoneFixups(gltf.scene);

  if (mapping) {
    try {
      patchMaterials(gltf.scene, mapping, new URL(mappingUrl, window.location.href));
    } catch (err) {
      console.warn('[viewer] material patch failed:', err);
    }
  }

  autoFrame(camera, controls, gltf.scene);

  const ro = new ResizeObserver(() => {
    const W = container.clientWidth;
    const H = container.clientHeight;
    if (W === 0 || H === 0) return;
    renderer.setSize(W, H, false);
    camera.aspect = W / H;
    camera.updateProjectionMatrix();
  });
  ro.observe(container);

  renderer.setAnimationLoop(() => {
    controls.update();
    renderer.render(scene, camera);
  });

  return { renderer, scene, camera, controls, gltf };
}

// ---------------------------------------------------------------- bone fixups

// Quaternions are stored three.js-order [x, y, z, w]. Blender pose-mode
// quaternions are (w, x, y, z) — convert when adding new entries.
//
// Tuned by hand in Blender pose mode then transcribed here; see issue #9 for
// the MH Chaos-Cloth-at-rest background. Only the root joint of each lace
// chain needs rotation — child joints inherit via the skeleton.
const BONE_FIXUPS = {
  // Taro hoodie drawstring — rotates both strings forward-down so they drape
  // against the chest instead of sticking out horizontally.
  'dyn_string_l_joint01': [0, 0.4828, 0, 0.8757],
  'dyn_string_r_joint01': [0, 0.4742, 0, 0.8804],
};

function applyBoneFixups(root) {
  let applied = 0;
  root.traverse((o) => {
    if (!o.isBone) return;
    const q = BONE_FIXUPS[o.name];
    if (!q) return;
    o.quaternion.set(q[0], q[1], q[2], q[3]);
    applied += 1;
  });
  if (applied > 0) {
    console.log('[viewer] applied', applied, 'bone fixup(s)');
  }
}

// --------------------------------------------------------------------- framing

function autoFrame(camera, controls, obj) {
  const box = new THREE.Box3().setFromObject(obj);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());

  // Aim slightly below the crown so the top of the head sits near the top of
  // the frame (not past it). MH body bbox usually extends feet→hair-peak; the
  // eye line is around 90% of height and hair peak around 98%.
  const headY = box.min.y + size.y * 0.88;
  // Head width is ~20% of body height → frame to that, not the full body.
  const headExtent = size.y * 0.20;
  const fov = (camera.fov * Math.PI) / 180;
  // Factor < 1 crops in past the bbox extent; 0.95 gives a tight portrait
  // with head just touching frame edges. Tweak if the model's hair pokes out.
  const dist = (headExtent / Math.tan(fov / 2)) * 0.95;

  camera.position.set(center.x, headY, center.z + dist);
  camera.near = Math.max(dist / 1000, 0.001);
  camera.far = dist * 200;
  camera.updateProjectionMatrix();
  controls.target.set(center.x, headY, center.z);
  controls.minDistance = dist * 0.4;
  controls.maxDistance = dist * 8;
  controls.update();
}

// -------------------------------------------------------------------- applySkin

// Baked skin settings, dialled in via the live panel then frozen here.
//   roughness floor  = 0.42   (max() against roughnessMap.g * roughness)
//   roughness bias   = 0.00
//   roughness mul    = 1.00   (material.roughness)
//   specularIntensity= 0.65
//   clearcoat        = 0.00
//   sheen            = 0.00
//   envMapIntensity  = 1.00   (default, left alone)
//   exposure         = 1.00   (default, left alone on renderer)
function applySkin(mat) {
  if ('metalness' in mat)          mat.metalness = 0;
  if ('roughness' in mat)          mat.roughness = 1.0;
  if ('specularIntensity' in mat)  mat.specularIntensity = 0.65;
  // Inject roughness-floor override: roughnessMap's scalar multiplier can only
  // darken below the baked value — `uRoughMin` clamps roughness up to a floor
  // so the baked-in oily hotspots on forehead/nose blur into matte.
  uniqueCacheKey(mat);
  const prevOBC = mat.onBeforeCompile;
  mat.onBeforeCompile = (shader) => {
    if (prevOBC) prevOBC(shader);
    shader.fragmentShader = shader.fragmentShader.replace(
      '#include <roughnessmap_fragment>',
      `
      float roughnessFactor = roughness;
      #ifdef USE_ROUGHNESSMAP
        vec4 texelRoughness = texture2D( roughnessMap, vRoughnessMapUv );
        roughnessFactor *= texelRoughness.g;
      #endif
      roughnessFactor = max( roughnessFactor, 0.42 );
      `
    );
  };
  mat.needsUpdate = true;
}

// --------------------------------------------------------------- material patch

function patchMaterials(root, mapping, baseUrl) {
  const specByName = new Map();
  for (const m of mapping.materials || []) {
    if (m.material_name) specByName.set(m.material_name, m);
  }

  const texLoader = new THREE.TextureLoader();
  const texCache = new Map();
  const loadTex = (relUrl, { srgb = false, flipY = false } = {}) => {
    if (!relUrl) return null;
    const key = `${relUrl}|${srgb}|${flipY}`;
    if (texCache.has(key)) return texCache.get(key);
    const url = new URL(relUrl, baseUrl).href;
    const tex = texLoader.load(url);
    tex.colorSpace = srgb ? THREE.SRGBColorSpace : THREE.NoColorSpace;
    tex.flipY = flipY;
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    texCache.set(key, tex);
    return tex;
  };

  const counts = { matched: 0, skipped: 0, byKind: {} };
  // Hair gets a render-time two-pass treatment (inner opaque alpha-clip +
  // outer translucent alpha-blend). Collect candidates in the traverse and
  // clone them after, so we don't mutate the scene graph while walking it.
  const hairTwoPass = [];
  root.traverse((obj) => {
    if (!obj.isMesh) return;
    const materials = Array.isArray(obj.material) ? obj.material : [obj.material];
    materials.forEach((mat, i) => {
      if (!mat?.name) return;
      const spec = specByName.get(mat.name);
      if (!spec) { counts.skipped += 1; return; }
      counts.matched += 1;
      const key = spec.face_slot ? `${spec.kind}:${spec.face_slot}` : spec.kind;
      counts.byKind[key] = (counts.byKind[key] || 0) + 1;
      const patched = applySpec(mat, spec, loadTex);
      if (patched && patched !== mat) {
        if (Array.isArray(obj.material)) obj.material[i] = patched;
        else obj.material = patched;
      }
      if (spec.kind === 'hair') {
        hairTwoPass.push({ mesh: obj, slot: i, outerMat: patched || mat, spec });
      }
      if (spec.kind === 'skin') {
        // Ensure MeshPhysicalMaterial so specularIntensity / IOR are real
        // properties, then bake the tuned skin settings. No live sliders —
        // values came from eyeballing in the panel, now fixed in code.
        let skinMat = patched || mat;
        if (!skinMat.isMeshPhysicalMaterial && skinMat.isMeshStandardMaterial) {
          const phys = new THREE.MeshPhysicalMaterial();
          phys.copy(skinMat);
          phys.name = skinMat.name;
          skinMat = phys;
          if (Array.isArray(obj.material)) obj.material[i] = phys;
          else obj.material = phys;
        }
        applySkin(skinMat);
      }
    });
  });
  for (const { mesh, outerMat, spec } of hairTwoPass) {
    addHairInnerPass(mesh, outerMat, spec);
  }
  console.log('[viewer] patched', counts.matched, 'matched /', counts.skipped,
              'skipped', counts.byKind, '/ hair two-pass:', hairTwoPass.length);
}

function applySpec(mat, spec, loadTex) {
  const p = spec.params || {};
  const t = spec.textures || {};
  switch (spec.kind) {
    case 'cloth':
      applyCloth(mat, p, t, loadTex);
      break;
    case 'hair':
      applyHair(mat, p, t, loadTex);
      break;
    case 'face_accessory':
      if (spec.face_slot === 'eye_refractive') {
        applyEyeRefractive(mat, p, t, loadTex);
      } else if (spec.face_slot === 'eyelashes') {
        applyEyelashes(mat, p, t, loadTex);
      } else {
        applyFaceAccessory(mat, spec, p, t, loadTex);
      }
      break;
    case 'skin':
    case 'generic':
    default:
      break;
  }
  mat.needsUpdate = true;
  return mat;
}

// Each onBeforeCompile hook needs a unique program cache key so three.js
// doesn't share one compiled shader across all patched materials (uniforms
// would collide — last one written wins).
function uniqueCacheKey(mat) {
  mat.customProgramCacheKey = () => mat.uuid;
}

// ---------------- cloth

function applyCloth(mat, p, t, loadTex) {
  const color1 = vecToColor(p.diffuse_color_1, 1, 1, 1);
  const color2 = vecToColor(p.diffuse_color_2, color1.r, color1.g, color1.b);

  const maskTex   = loadTex(t.mask,           { srgb: false });
  const detailTex = loadTex(t.detail_diffuse, { srgb: true  });
  const macroTex  = loadTex(t.macro_overlay,  { srgb: true  });
  const aoTex     = loadTex(t.ao,             { srgb: false });

  if (typeof p.roughness === 'number') mat.roughness = p.roughness;
  if (typeof p.metallic === 'number')  mat.metalness = p.metallic;

  // Force USE_MAP so three.js emits vMapUv; we override map sampling below.
  mat.map = maskTex || detailTex || aoTex || macroTex || mat.map;
  if (mat.map) mat.map.colorSpace = THREE.NoColorSpace;

  const uniforms = {
    uClothColor1:    { value: color1 },
    uClothColor2:    { value: color2 },
    uMaskTex:        { value: maskTex || null },
    uMaskOn:         { value: maskTex ? 1.0 : 0.0 },
    uMaskChannel:    { value: channelIndex(p.mask_channel) },
    uAoTex:          { value: aoTex || null },
    uAoOn:           { value: aoTex ? 1.0 : 0.0 },
    uAoAmount:       { value: p.ao_amount ?? 0.85 },
    uDetailTex:      { value: detailTex || null },
    uDetailOn:       { value: detailTex ? 1.0 : 0.0 },
    uDetailTiling:   { value: clamp(p.detail_tiling ?? 80.0, 1, 400) },
    uDetailStrength: { value: p.detail_strength ?? 0.6 },
    uMacroTex:       { value: macroTex || null },
    uMacroOn:        { value: macroTex ? 1.0 : 0.0 },
    uMacroTiling:    { value: clamp(p.macro_tiling ?? 3.0, 0.1, 20) },
    uMacroStrength:  { value: p.macro_strength ?? 0.5 },
  };

  uniqueCacheKey(mat);
  mat.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);
    shader.fragmentShader = `
      uniform vec3  uClothColor1;
      uniform vec3  uClothColor2;
      uniform sampler2D uMaskTex;
      uniform float uMaskOn;
      uniform int   uMaskChannel;
      uniform sampler2D uAoTex;
      uniform float uAoOn;
      uniform float uAoAmount;
      uniform sampler2D uDetailTex;
      uniform float uDetailOn;
      uniform float uDetailTiling;
      uniform float uDetailStrength;
      uniform sampler2D uMacroTex;
      uniform float uMacroOn;
      uniform float uMacroTiling;
      uniform float uMacroStrength;
      float pickChannel(vec4 v, int c) {
        if (c == 1) return v.g;
        if (c == 2) return v.b;
        if (c == 3) return v.a;
        return v.r;
      }
    ` + shader.fragmentShader;

    shader.fragmentShader = shader.fragmentShader.replace(
      '#include <map_fragment>',
      `
      vec2 clothUv = vMapUv;
      float maskV = 0.0;
      if (uMaskOn > 0.5) maskV = pickChannel(texture2D(uMaskTex, clothUv), uMaskChannel);
      vec3 clothBase = mix(uClothColor1, uClothColor2, maskV);
      if (uDetailOn > 0.5) {
        vec3 detail = texture2D(uDetailTex, clothUv * uDetailTiling).rgb;
        clothBase *= mix(vec3(1.0), detail * 2.0, uDetailStrength);
      }
      if (uMacroOn > 0.5) {
        vec3 macro = texture2D(uMacroTex, clothUv * uMacroTiling).rgb;
        vec3 ov = mix(
          vec3(1.0) - 2.0 * (vec3(1.0) - clothBase) * (vec3(1.0) - macro),
          2.0 * clothBase * macro,
          step(clothBase, vec3(0.5))
        );
        clothBase = mix(clothBase, ov, uMacroStrength);
      }
      if (uAoOn > 0.5) {
        float ao = texture2D(uAoTex, clothUv).r;
        clothBase *= mix(1.0, ao, uAoAmount);
      }
      diffuseColor.rgb = clothBase;
      `
    );
  };
}

// ---------------- hair

function applyHair(mat, p, t, loadTex) {
  // The glTF-carried map is the data-packed hair card atlas (R=strand mask on
  // compact atlases, A=legacy) — NOT albedo. Drop it; color comes from params.
  if (p.ignore_gltf_map) mat.map = null;
  if (p.base_color) mat.color.setRGB(p.base_color[0], p.base_color[1], p.base_color[2]);
  if (typeof p.roughness === 'number') mat.roughness = p.roughness;

  // Try sidecar atlas first (via stem in params), then fall back to whatever
  // stage 03 rewrote into the textures dict (full relative URL).
  const atlasUrl = p.alpha_stem ? `textures/${p.alpha_stem}.png`
                 : (t.alpha_r || t.alpha || null);
  const atlas = atlasUrl ? loadTex(atlasUrl, { srgb: false }) : null;
  if (!atlas) {
    if (p.alpha_clip) { mat.alphaTest = 0.5; mat.side = THREE.DoubleSide; }
    console.warn('[viewer][hair]', mat.name, 'no atlas — using hard cutoff');
    return;
  }

  // Two-pass hair: this material is the OUTER pass — translucent alpha-blend
  // over the alphaMap channel, depth-write disabled. An inner opaque alpha-
  // clip sibling is added by patchMaterials (addHairInnerPass) so the core
  // silhouette writes depth reliably and the outer fringe blends on top.
  mat.alphaMap = atlas;
  mat.alphaHash = false;
  mat.transparent = true;
  mat.alphaTest = 0.0;
  mat.depthWrite = false;
  mat.side = THREE.DoubleSide;

  const chan = (p.alpha_channel || 'r').toLowerCase();
  const swizzle = ({ r: 'r', g: 'g', b: 'b', a: 'a' }[chan]) || 'r';

  uniqueCacheKey(mat);
  mat.onBeforeCompile = (shader) => {
    // Replace the default alphaMap sampler (which uses .g) with the declared
    // channel so MH compact-atlas R gets picked up, not the dense G gradient.
    shader.fragmentShader = shader.fragmentShader.replace(
      '#include <alphamap_fragment>',
      `
      #ifdef USE_ALPHAMAP
        diffuseColor.a *= texture2D( alphaMap, vAlphaMapUv ).${swizzle};
      #endif
      `
    );
  };
  console.log('[viewer][hair]', mat.name, '→ outer alpha-blend R-channel, atlas=' + atlasUrl);
}

// Build the inner opaque-alpha-clip sibling for a hair mesh.
// Shares BufferGeometry and the alphaMap Texture with the outer pass — no
// extra VRAM for geometry or textures. Only the material is cloned.
function addHairInnerPass(mesh, outerMat, spec) {
  if (!outerMat?.alphaMap) return;         // nothing to clip against
  if (Array.isArray(mesh.material)) return; // multi-slot hair meshes: skip for now
  const p = spec.params || {};
  const chan = (p.alpha_channel || 'r').toLowerCase();
  const swizzle = ({ r: 'r', g: 'g', b: 'b', a: 'a' }[chan]) || 'r';
  const threshold = typeof p.inner_alpha_threshold === 'number'
                    ? p.inner_alpha_threshold : 0.5;

  const innerMat = outerMat.clone();
  innerMat.name = (outerMat.name || 'hair') + '__inner';
  // Alpha-clip core: opaque bucket, depth-writes, sharp threshold on R.
  innerMat.alphaHash = false;
  innerMat.transparent = false;
  innerMat.depthWrite = true;
  innerMat.alphaTest = threshold;
  innerMat.side = THREE.DoubleSide;

  uniqueCacheKey(innerMat);
  innerMat.onBeforeCompile = (shader) => {
    shader.fragmentShader = shader.fragmentShader.replace(
      '#include <alphamap_fragment>',
      `
      #ifdef USE_ALPHAMAP
        diffuseColor.a *= texture2D( alphaMap, vAlphaMapUv ).${swizzle};
      #endif
      `
    );
  };
  innerMat.needsUpdate = true;

  // mesh.clone() shares BufferGeometry (no VRAM cost) and correctly copies
  // skeleton binding for SkinnedMesh so the clone deforms with the rig.
  const inner = mesh.clone();
  inner.material = innerMat;
  inner.name = mesh.name + '__inner';
  mesh.parent?.add(inner);
  console.log('[viewer][hair] +inner pass for', mesh.name, 'alphaTest=' + threshold);
}

// ---------------- eye refractive

function applyEyeRefractive(mat, p, t, loadTex) {
  // t.basecolor is already a sidecar-relative URL ("textures/X.png") because
  // stage 03 rewrites the textures dict values. Don't prefix again.
  const atlas = t.basecolor ? loadTex(t.basecolor, { srgb: false }) : null;
  if (!atlas) {
    // No iris atlas — just set sclera-ish.
    if (p.sclera_color) mat.color.setRGB(p.sclera_color[0], p.sclera_color[1], p.sclera_color[2]);
    mat.roughness = 0.25;
    return;
  }
  atlas.wrapS = atlas.wrapT = THREE.ClampToEdgeWrapping;  // EXTEND equivalent

  const [pu, pv] = p.eye_pole_uv || [0.5, 0.5];

  // Ensure mat.map is populated so vMapUv is emitted by the shader.
  mat.map = atlas;
  mat.map.colorSpace = THREE.NoColorSpace;
  // MH eyes are wet — almost mirror roughness + clearcoat layer for specular
  // highlights over the iris/sclera. (MeshPhysicalMaterial supports these.)
  mat.roughness = 0.05;
  mat.metalness = 0.0;
  if ('clearcoat' in mat) {
    mat.clearcoat = 1.0;
    mat.clearcoatRoughness = 0.03;
  }

  const uniforms = {
    uIrisAtlas:       { value: atlas },
    uPole:            { value: new THREE.Vector2(pu, pv) },
    uUvScale:         { value: p.uv_scale ?? 4.0 },
    uIrisColor:       { value: vecToColor(p.iris_color,   0.176, 0.077, 0.045) },
    uIrisDark:        { value: vecToColor(p.iris_dark,    0.036, 0.015, 0.000) },
    uScleraColor:     { value: vecToColor(p.sclera_color, 0.92,  0.90,  0.86 ) },
    uPupilColor:      { value: vecToColor(p.pupil_color,  0.005, 0.005, 0.005) },
    uVeinColor:       { value: vecToColor(p.vein_color,   0.62,  0.18,  0.15 ) },
    uVeinsPower:      { value: p.veins_power ?? 0.5 },
    uLimbusStart:     { value: p.limbus_start ?? 0.095 },
    uIrisRadIn:       { value: p.iris_radius_in ?? 0.131 },
    uIrisRadOut:      { value: p.iris_radius_out ?? 0.146 },
    uPupilRadIn:      { value: p.pupil_radius_in ?? 0.022 },
    uPupilRadOut:     { value: p.pupil_radius_out ?? 0.046 },
    uFibrilInMin:     { value: p.fibril_from_min ?? 0.25 },
    uFibrilInMax:     { value: p.fibril_from_max ?? 0.75 },
    uFibrilOutMin:    { value: p.fibril_to_min ?? 0.65 },
    uFibrilOutMax:    { value: p.fibril_to_max ?? 1.15 },
  };

  uniqueCacheKey(mat);
  mat.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);

    shader.fragmentShader = `
      uniform sampler2D uIrisAtlas;
      uniform vec2  uPole;
      uniform float uUvScale;
      uniform vec3  uIrisColor;
      uniform vec3  uIrisDark;
      uniform vec3  uScleraColor;
      uniform vec3  uPupilColor;
      uniform vec3  uVeinColor;
      uniform float uVeinsPower;
      uniform float uLimbusStart;
      uniform float uIrisRadIn;
      uniform float uIrisRadOut;
      uniform float uPupilRadIn;
      uniform float uPupilRadOut;
      uniform float uFibrilInMin;
      uniform float uFibrilInMax;
      uniform float uFibrilOutMin;
      uniform float uFibrilOutMax;

      float mhRemap(float x, float a, float b, float c, float d) {
        float t = clamp((x - a) / max(b - a, 1e-6), 0.0, 1.0);
        return mix(c, d, t);
      }
      // Inverted ramp — fac=1 inside pupil, 0 outside.
      float mhPupilRamp(float r, float a, float b) {
        return 1.0 - smoothstep(a, b, r);
      }
      float mhLimbusRamp(float b) {
        // 0.095 → 0.0 ... 1.0 → 1.0
        return clamp((b - uLimbusStart) / max(1.0 - uLimbusStart, 1e-6), 0.0, 1.0);
      }
      // Cheap pseudo-voronoi-distance-to-edge for sclera veins. Cellular noise
      // approximated with a hash; distance to the nearest cell boundary reads
      // as a branching web. Not pixel-perfect but visually in family.
      vec2  mhHash2(vec2 p) {
        p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
        return fract(sin(p) * 43758.5453);
      }
      float mhVoronoiEdge(vec2 p) {
        vec2 n = floor(p);
        vec2 f = fract(p);
        float md = 1.0;
        vec2  mr;
        for (int j = -1; j <= 1; j++) {
          for (int i = -1; i <= 1; i++) {
            vec2 g = vec2(float(i), float(j));
            vec2 o = mhHash2(n + g);
            vec2 r = g + o - f;
            float d = dot(r, r);
            if (d < md) { md = d; mr = r; }
          }
        }
        // distance to edge approximation
        md = 8.0;
        for (int j = -2; j <= 2; j++) {
          for (int i = -2; i <= 2; i++) {
            vec2 g = vec2(float(i), float(j));
            vec2 o = mhHash2(n + g);
            vec2 r = g + o - f;
            if (dot(mr - r, mr - r) > 1e-5) {
              md = min(md, dot(0.5 * (mr + r) - mr, normalize(r - mr)));
            }
          }
        }
        return md;
      }
    ` + shader.fragmentShader;

    shader.fragmentShader = shader.fragmentShader.replace(
      '#include <map_fragment>',
      `
      // ---- MH eye reconstruction ----
      vec2 meshUv = vMapUv;
      // Iris texture sampled with UV_SCALE zoom centered at pole UV → (0.5, 0.5).
      vec2 atlasUv = vec2(0.5) + (meshUv - uPole) * uUvScale;
      vec4 irisTex = texture2D(uIrisAtlas, clamp(atlasUv, vec2(0.001), vec2(0.999)));

      // Fibril detail from R channel (mapRange 0.25..0.75 → 0.65..1.15).
      float fib = mhRemap(irisTex.r, uFibrilInMin, uFibrilInMax, uFibrilOutMin, uFibrilOutMax);
      vec3 irisFibril = uIrisColor * vec3(fib);

      // Limbus darkening from B channel.
      float limbusFac = mhLimbusRamp(irisTex.b);
      vec3 irisCol = mix(irisFibril, uIrisDark, limbusFac);

      // Radial distance in mesh-UV space from pole.
      float rd = length(meshUv - uPole);

      // Iris vs sclera ramp (hard boundary).
      float irisMask = smoothstep(uIrisRadIn, uIrisRadOut, rd);  // 0 inside iris, 1 outside

      // Sclera veins (procedural voronoi-edge, masked to sclera, scaled by power).
      float vEdge = 1.0 - smoothstep(0.0, 0.04, mhVoronoiEdge(meshUv * 35.0));
      float veinMask = clamp(vEdge * irisMask * clamp(uVeinsPower, 0.0, 1.0), 0.0, 1.0);
      vec3 scleraCol = mix(uScleraColor, uVeinColor, veinMask);

      // Iris inside, sclera outside.
      vec3 irisOrSclera = mix(irisCol, scleraCol, irisMask);

      // Pupil at center.
      float pupilFac = mhPupilRamp(rd, uPupilRadIn, uPupilRadOut);
      vec3 eyeCol = mix(irisOrSclera, uPupilColor, pupilFac);

      diffuseColor.rgb = eyeCol;
      `
    );
  };
}

// ---------------- eyelashes

function applyEyelashes(mat, p, t, loadTex) {
  if (p.base_color) mat.color.setRGB(p.base_color[0], p.base_color[1], p.base_color[2]);
  if (typeof p.roughness === 'number') mat.roughness = p.roughness;
  mat.side = THREE.DoubleSide;
  // Drop any glTF-carried map — eyelash coverage is alpha-only, not color.
  mat.map = null;

  // t.alpha is already a sidecar-relative URL ("textures/X.png").
  const alpha = t.alpha ? loadTex(t.alpha, { srgb: false }) : null;
  if (!alpha) {
    console.warn('[viewer][lash]', mat.name, 'no coverage texture');
    if (p.alpha_clip) mat.alphaTest = 0.5;
    return;
  }

  // Eyelash coverage PNGs are grayscale (R=G=B=A). Default alphaMap .g works.
  mat.alphaMap = alpha;
  mat.alphaHash = true;
  mat.transparent = false;
  mat.alphaTest = 0.0;
  mat.depthWrite = true;
  console.log('[viewer][lash]', mat.name, '→ alphaHash .g, coverage=' + t.alpha);
}

// ---------------- face accessory (teeth / saliva / eyeshell / eyeEdge / cartilage)

function applyFaceAccessory(mat, spec, p, t, loadTex) {
  const slot = spec.face_slot;

  // Slots we can't credibly reproduce without UE refraction/caustics/SSS:
  //   wet      — saliva film + eyeEdge waterline
  //   cartilage — tear-duct caruncle (pink inner-rim tissue)
  // Suppress rendering entirely. three.js Material has no .visible, so blank
  // out both color and depth writes.
  if (slot === 'wet' || slot === 'cartilage') {
    mat.transparent = true;
    mat.opacity = 0;
    mat.colorWrite = false;
    mat.depthWrite = false;
    mat.side = THREE.DoubleSide;
    return;
  }

  if (p.base_color) mat.color.setRGB(p.base_color[0], p.base_color[1], p.base_color[2]);
  if (typeof p.roughness === 'number') mat.roughness = p.roughness;
  if (p.alpha_clip) { mat.alphaTest = 0.5; mat.side = THREE.DoubleSide; }

  if (slot === 'teeth') {
    mat.roughness = 0.35;
  }
  if (slot === 'eye_occlusion') {
    // Dark ring under the lid that sells socket depth.
    mat.color.setRGB(0.02, 0.015, 0.01);
    mat.transparent = true;
    mat.opacity = 0.4;
    mat.roughness = 0.8;
    mat.depthWrite = false;
  }
  if (slot === 'cartilage') {
    mat.roughness = 0.6;
  }
}

// ---------------- helpers

function vecToColor(vec, fr, fg, fb) {
  if (vec && vec.length >= 3) return new THREE.Color(vec[0], vec[1], vec[2]);
  return new THREE.Color(fr, fg, fb);
}
function channelIndex(name) {
  switch ((name || 'r').toLowerCase()) {
    case 'g': return 1;
    case 'b': return 2;
    case 'a': return 3;
    default: return 0;
  }
}
function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }
