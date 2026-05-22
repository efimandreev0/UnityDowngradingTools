# UnityDowngradingTools
These tools was written specially for Sally Face on PSVita, because AssetRipperGUI decompiling a bit shitty, and when I trying to compile game it just going to bootloop with "Do not use ReadObjectThreaded". This tools may help fix shit like this.
# Fixing an AssetRipper decompile for an older Unity (PS Vita)

Notes from porting a **Sally Face** decompile to **Unity 2018.2.21f1** for PS Vita.
Most of it applies to any AssetRipper decompile that needs to run on an older
Unity than it was exported for.

Scripts live in the repo root. The project itself is in `ExportedProject/`.

## Requirements

- Python 3.8+
- ffmpeg (any recent build, portable is fine) — for the audio fix
- ~30 GB free (project backup + Library rebuild)

## Before you start

Make a backup — steps 6 and 8 read original references from it:

```powershell
Copy-Item "ExportedProject" "ExportedProject - backup" -Recurse
```

Close Unity.

## 1. Downgrade file formats

AssetRipper exports in the 2018.3+ format even when ProjectVersion.txt says
2018.2. That trips the serializer with `ReadObjectThreaded on scene objects!`.

Renames `m_CorrespondingSourceObject` to `m_PrefabParentObject`, lowers
`serializedVersion` values, drops `m_PrefabAsset`, physical-camera fields,
`m_DynamicOccludee` on SpriteRenderer, etc.

```powershell
python fix_unity_project.py --project "ExportedProject" --target 2018.2 --wipe-library
```

- `--target 2018.2` — most aggressive downgrade; use `--target 2018.4` for 2018.4 LTS
- `--wipe-library` — delete `Library/` at the end
- `--dry-run` — preview without writing

Around 62,000 files change. The regex pass sometimes stops short inside the
large `MonoBehaviour/` folder — the next step covers what it misses.

## 2. Finish the asset pass

```powershell
python fix_remaining_assets.py "ExportedProject"
python cleanup_and_finish.py --project "ExportedProject"
```

`fix_remaining_assets.py` uses a flat byte replace, so it reliably covers all
60k+ AC MonoBehaviour assets. `cleanup_and_finish.py` also handles
`.controller`/`.anim`/`.mat`/`.guiskin`, removes `.bak` leftovers (Unity treats
them as assets), and lowers `serializedVersion` in `GraphicsSettings.asset` and
`ProjectSettings.asset`.

## 3. Fix the .wav files

AssetRipper writes wav files with zeroed chunk sizes in the RIFF header, which
gives `FSBTool ERROR: Failed decoding audio clip`. ffmpeg re-encodes them.

```powershell
python fix_audio.py --project "ExportedProject" --ffmpeg "path\to\ffmpeg.exe"
```

- `--ffmpeg <path>` — path to ffmpeg.exe if not on PATH
- `--jobs N` — parallelism (default: CPU count)
- `--dry-run` — preview

.ogg files are fine and left alone.

## 4. Strip baked lighting

`LightingData.asset` from 2018.3+ uses an incompatible `serializedVersion: 4`
and breaks the build pipeline. A 2D game does not need baked lighting.

```powershell
python strip_lighting.py --project "ExportedProject"
```

Removes `LightingData.asset`, `LightProbes.asset`, `NavMesh.asset` and clears
references to them in scenes.

## 5. Clear cross-scene direct references

AssetRipper keeps direct references from `.asset` files (AC Actions) to specific
GameObjects inside `.unity` scenes. Unity cannot resolve those, hence
`ReadObjectThreaded on scene objects!`.

```powershell
python strip_scene_refs.py "ExportedProject"
```

After this, AC resolves targets through ConstantID only — the next steps restore
that.

## 6. Restore ConstantID links

Reads original direct references from the backup.

```powershell
python restore_constant_ids.py --current "ExportedProject" --backup "ExportedProject - backup"
```

It parses the AC sources under `Assets/Scripts/.../AC/*.cs` for
`AssignFile(..., idField, refField)` calls to get the exact field pairing (for
ActionTeleport, `teleporter` maps to `markerID`), parses every `.unity` scene for
a global `(scene_guid, fileID) -> constantID` map, then patches the correct ID
field in each `.asset`.

## 7. Reset assets if a restore went wrong

If a restore wrote to the wrong field, reset and rerun step 6:

```powershell
python reset_assets_from_backup.py "ExportedProject" "ExportedProject - backup"
```

This takes `.asset` files from the backup and reapplies steps 1, 2 and 5 only —
the state right before step 6.

## 8. Add missing ConstantID components

AC did not always use ConstantID; some GameObjects were resolved through direct
references, which do not work in Unity. This adds a ConstantID component to those
GameObjects.

```powershell
python add_constant_ids.py --current "ExportedProject" --backup "ExportedProject - backup" --scenes all
```

`--scenes all` processes every scene; pass scene names to limit it
(`--scenes lvl00 lvl01 mainMenu`).

For each scene it collects the target fileIDs that Actions referenced, adds a
ConstantID component where one is missing (reusing the number already in the
`.asset` so the pass is idempotent), registers it in the GameObject's
`m_Component` list, and patches the matching `*constantID` field.

Each scene gets a `.unity.bak3` next to it for rollback.

After this, run `fix_orphan_components.py` once — it registers any ConstantID
component that ended up in a scene without a `m_Component` entry:

```powershell
python fix_orphan_components.py "ExportedProject"
```

## 9. First Unity import

```powershell
Remove-Item -Recurse -Force "ExportedProject\Library","ExportedProject\Temp","ExportedProject\obj" -ErrorAction SilentlyContinue
```

Open Unity (2018.2.21f1 or 2018.4 LTS). The first import takes one to two hours
on 60k+ assets.

In the "API Update Required" dialog choose "I Made a Backup. Go Ahead!".

Player Settings:
- Scripting Backend: IL2CPP
- Api Compatibility Level: .NET Standard 2.0, or .NET 4.x if something is missing
- Managed Stripping Level: Disabled or Low — AC uses reflection, high stripping
  breaks it

Quality Settings: Edit > Project Settings > Quality, for each level set Other >
Blend Weights to 4 Bones to avoid `Bone weights do not match bones` warnings.

## 10. Build settings

Build Settings > PS Vita.

Debug build:
- Build Type: PC Hosted
- Compress with PSArc: off
- Development Build: on
- Script Debugging: on
- Explicit Null Checks: on
- Array Bounds Checks: on
- Scripts Only Build: off

Release build:
- Build Type: Package
- Compress with PSArc: on
- everything else: off

## Downgrading textures for PS Vita

The Vita has 512 MB RAM and 128 MB VRAM. Decompiled textures are usually
uncompressed RGBA32 and need downsizing.

Per texture (Inspector):

- Max Size: 1024 for UI/sprites, 2048 only for large backgrounds
- Compression: PVRTC RGB(A) 4 bits, or 2 bits for UI/decals
- Crunch Compression: off — the Vita has no runtime crunch decode
- Read/Write Enabled: off unless code reads pixels
- Generate Mipmaps: on for 3D, off for UI/sprites
- Aniso Level: 1

For a per-platform override, use the PS Vita tab in the importer and enable
"Override for PS Vita".

To do it in bulk, add `Assets/Editor/VitaTextureSettings.cs`:

```csharp
using UnityEditor;
using UnityEngine;

public class VitaTextureSettings
{
    [MenuItem("Tools/Vita/Apply Texture Settings to All")]
    public static void ApplyAll()
    {
        string[] guids = AssetDatabase.FindAssets("t:Texture2D");
        int n = 0;
        foreach (string guid in guids)
        {
            string path = AssetDatabase.GUIDToAssetPath(guid);
            var ti = AssetImporter.GetAtPath(path) as TextureImporter;
            if (ti == null) continue;

            ti.isReadable = false;
            ti.crunchedCompression = false;
            ti.mipmapEnabled = ti.textureType != TextureImporterType.Sprite;

            ti.SetPlatformTextureSettings(new TextureImporterPlatformSettings
            {
                name = "PSP2",
                overridden = true,
                maxTextureSize = 1024,
                format = TextureImporterFormat.PVRTC_RGBA4,
                textureCompression = TextureImporterCompression.Compressed,
                compressionQuality = 50,
            });

            var def = ti.GetDefaultPlatformTextureSettings();
            def.maxTextureSize = 1024;
            ti.SetPlatformTextureSettings(def);

            ti.SaveAndReimport();
            n++;
        }
        Debug.Log($"Vita texture settings applied to {n} textures.");
    }
}
```

Run it from Tools > Vita > Apply Texture Settings to All. `"PSP2"` is the Vita
platform key.

Also worth doing:
- Audio: Load Type Compressed In Memory (or Streaming for long clips), Vorbis
  format, 30-50% quality
- Meshes: Mesh Compression Medium or High in the model importer
- Build: Compression Method LZ4HC for the release build

## Diagnostics

```powershell
# 2018.3+ fields still present (should be none)
Get-ChildItem -Path "ExportedProject\Assets" -Recurse -Include "*.asset","*.unity","*.prefab" |
  Select-String "m_CorrespondingSourceObject|m_PrefabAsset:" | Select-Object -First 5

# scene refs left in .asset (should be 0)
Get-ChildItem -Path "ExportedProject\Assets\MonoBehaviour" -Filter "*.asset" |
  Select-String '\{fileID:\s*[1-9]\d*,\s*guid:\s*[0-9a-f]{32},\s*type:\s*2\}' | Measure-Object
```

## Step summary

```
0. Backup, close Unity
1. fix_unity_project.py --target 2018.2 --wipe-library
2. fix_remaining_assets.py, cleanup_and_finish.py
3. fix_audio.py
4. strip_lighting.py
5. strip_scene_refs.py
6. restore_constant_ids.py
7. (only if step 6 went wrong: reset_assets_from_backup.py, then step 6 again)
8. add_constant_ids.py --scenes all, then fix_orphan_components.py
9. delete Library/, open Unity
10. Player/Quality/Build settings
```

## Not fixed automatically

- Missing scripts on individual components (Console warning, not fatal)
- AC Actions resolved by name without ConstantID — fix those in the Unity editor
- Shaders from removed modules — Unity falls back to defaults
- AssetRipper dummy shaders (`Assets/Shader/*.shader`) — delete them, they fail
  to compile on PSP2; Unity uses the built-in versions

All scripts are idempotent and safe to rerun.
