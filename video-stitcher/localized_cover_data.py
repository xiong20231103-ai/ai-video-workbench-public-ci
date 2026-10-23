"""Localized material names and short cover taglines."""

LANGUAGES = {
    "de": "德语 / Deutsch", "en": "英语 / English", "es": "西语 / Español",
    "pt": "葡语 / Português (Brasil)", "fr": "法语 / Français", "it": "意大利语 / Italiano",
    "ru": "俄语 / Русский", "id": "印尼语 / Bahasa Indonesia", "tr": "土耳其语 / Türkçe",
    "ko": "韩语 / 한국어",
}
LANGUAGE_LABEL_TO_CODE = {label: code for code, label in LANGUAGES.items()}
MATERIALS = {
    "white_panel": "净白板", "carbon_crystal": "碳岩板", "metal_panel": "金属板",
    "mirror_panel": "镜面板", "wood_veneer": "木饰面", "pu_stone": "PU石",
    "flexible_pu_stone": "PU软石", "plastic_wood_flooring": "塑木地板",
    "crystal_rock_wall": "晶岩墙板",
}
MATERIAL_SERIES = {
    "seven": {"label": "七类材料（ProjectA/ProjectB）", "materials": (
        "white_panel", "carbon_crystal", "metal_panel", "mirror_panel", "wood_veneer", "pu_stone", "flexible_pu_stone")},
    "spc": {"label": "SPC材料（塑木地板/晶岩墙板）", "materials": ("plastic_wood_flooring", "crystal_rock_wall")},
}
SERIES_LABEL_TO_KEY = {item["label"]: key for key, item in MATERIAL_SERIES.items()}
MATERIAL_LABEL_TO_KEY = {label: key for key, label in MATERIALS.items()}

def _copy(title, subtitle):
    return {"title": title, "subtitle": subtitle}

LOCALIZED_COVER_COPY = {
    "en": {
        "white_panel": _copy("WHITE DECORATIVE PANEL", "CLEAN TILE LOOK\nEASY INSTALLATION"),
        "carbon_crystal": _copy("CARBON CRYSTAL PANEL", "PREMIUM MARBLE LOOK\nEASIER TO INSTALL"),
        "metal_panel": _copy("METAL PANEL", "BRUSHED METAL FINISH\nMODERN PREMIUM STYLE"),
        "mirror_panel": _copy("MIRROR PANEL", "BRIGHT MIRROR EFFECT\nSAFER FOR INTERIORS"),
        "wood_veneer": _copy("WOOD VENEER PANEL", "NATURAL WOOD LOOK\nWARM PREMIUM FINISH"),
        "pu_stone": _copy("PU STONE", "REAL STONE LOOK\nLIGHTWEIGHT, EASY TO INSTALL"),
        "flexible_pu_stone": _copy("FLEXIBLE PU STONE", "REAL STONE LOOK\nFLEXIBLE FOR CURVED WALLS"),
        "plastic_wood_flooring": _copy("PLASTIC WOOD FLOORING", "NATURAL WOOD LOOK\nWATERPROOF, EASY CARE"),
        "crystal_rock_wall": _copy("CRYSTAL ROCK WALL PANEL", "PREMIUM STONE LOOK\nWATERPROOF, EASY CARE"),
    },
    "de": {
        "white_panel": _copy("WEISSES DEKORPANEEL", "SAUBERE FLIESENOPTIK\nEINFACHE MONTAGE"),
        "carbon_crystal": _copy("CARBON CRYSTAL BOARD", "EDLE MARMOROPTIK\nEINFACHERE MONTAGE"),
        "metal_panel": _copy("METALLPANEEL", "GEBÜRSTETE METALLOPTIK\nMODERN UND HOCHWERTIG"),
        "mirror_panel": _copy("SPIEGELPANEEL", "HELLER SPIEGELEFFEKT\nSICHERER IM INNENRAUM"),
        "wood_veneer": _copy("HOLZFURNIER-PANEEL", "NATÜRLICHE HOLZOPTIK\nWARM UND HOCHWERTIG"),
        "pu_stone": _copy("PU-STEIN", "ECHTE STEINOPTIK\nLEICHT, EINFACH ZU MONTIEREN"),
        "flexible_pu_stone": _copy("FLEXIBLER PU-STEIN", "ECHTE STEINOPTIK\nFLEXIBEL FÜR RUNDE WÄNDE"),
        "plastic_wood_flooring": _copy("KUNSTSTOFF-HOLZBODEN", "NATÜRLICHE HOLZOPTIK\nWASSERFEST, PFLEGELEICHT"),
        "crystal_rock_wall": _copy("KRISTALLSTEIN-WANDPANEEL", "EDLE STEINOPTIK\nWASSERFEST, PFLEGELEICHT"),
    },
    "es": {
        "white_panel": _copy("PANEL DECORATIVO BLANCO", "ACABADO LIMPIO TIPO AZULEJO\nINSTALACIÓN FÁCIL"),
        "carbon_crystal": _copy("PANEL CARBON CRYSTAL", "EFECTO MÁRMOL PREMIUM\nINSTALACIÓN MÁS FÁCIL"),
        "metal_panel": _copy("PANEL METÁLICO", "ACABADO METÁLICO CEPILLADO\nESTILO MODERNO PREMIUM"),
        "mirror_panel": _copy("PANEL ESPEJO", "EFECTO ESPEJO LUMINOSO\nMÁS SEGURO EN INTERIORES"),
        "wood_veneer": _copy("PANEL DE MADERA", "ASPECTO DE MADERA NATURAL\nACABADO CÁLIDO PREMIUM"),
        "pu_stone": _copy("PIEDRA PU", "ASPECTO DE PIEDRA REAL\nLIGERA Y FÁCIL DE INSTALAR"),
        "flexible_pu_stone": _copy("PIEDRA FLEXIBLE DE PU", "ASPECTO DE PIEDRA REAL\nFLEXIBLE PARA MUROS CURVOS"),
        "plastic_wood_flooring": _copy("PISO DE MADERA PLÁSTICA", "ASPECTO DE MADERA NATURAL\nIMPERMEABLE Y FÁCIL DE CUIDAR"),
        "crystal_rock_wall": _copy("PANEL DE PARED CRYSTAL ROCK", "ACABADO DE PIEDRA PREMIUM\nIMPERMEABLE Y FÁCIL DE CUIDAR"),
    },
    "pt": {
        "white_panel": _copy("PAINEL DECORATIVO BRANCO", "VISUAL LIMPO DE AZULEJO\nINSTALAÇÃO FÁCIL"),
        "carbon_crystal": _copy("PAINEL CARBON CRYSTAL", "EFEITO MÁRMORE PREMIUM\nINSTALAÇÃO MAIS FÁCIL"),
        "metal_panel": _copy("PAINEL METÁLICO", "ACABAMENTO METÁLICO ESCOVADO\nESTILO MODERNO PREMIUM"),
        "mirror_panel": _copy("PAINEL ESPELHADO", "EFEITO ESPELHO LUMINOSO\nMAIS SEGURO EM INTERIORES"),
        "wood_veneer": _copy("PAINEL AMADEIRADO", "VISUAL DE MADEIRA NATURAL\nACABAMENTO QUENTE PREMIUM"),
        "pu_stone": _copy("PEDRA PU", "VISUAL DE PEDRA REAL\nLEVE E FÁCIL DE INSTALAR"),
        "flexible_pu_stone": _copy("PEDRA FLEXÍVEL PU", "VISUAL DE PEDRA REAL\nFLEXÍVEL PARA PAREDES CURVAS"),
        "plastic_wood_flooring": _copy("PISO DE MADEIRA PLÁSTICA", "VISUAL DE MADEIRA NATURAL\nIMPERMEÁVEL E FÁCIL DE CUIDAR"),
        "crystal_rock_wall": _copy("PAINEL DE PAREDE CRYSTAL ROCK", "ACABAMENTO DE PEDRA PREMIUM\nIMPERMEÁVEL E FÁCIL DE CUIDAR"),
    },
    "fr": {
        "white_panel": _copy("PANNEAU DÉCORATIF BLANC", "EFFET CARRELAGE PROPRE\nINSTALLATION FACILE"),
        "carbon_crystal": _copy("PANNEAU CARBON CRYSTAL", "EFFET MARBRE HAUT DE GAMME\nPOSE PLUS FACILE"),
        "metal_panel": _copy("PANNEAU MÉTALLIQUE", "FINITION MÉTAL BROSSÉ\nSTYLE MODERNE HAUT DE GAMME"),
        "mirror_panel": _copy("PANNEAU MIROIR", "EFFET MIROIR LUMINEUX\nPLUS SÛR EN INTÉRIEUR"),
        "wood_veneer": _copy("PANNEAU PLACAGE BOIS", "ASPECT BOIS NATUREL\nFINITION CHALEUREUSE PREMIUM"),
        "pu_stone": _copy("PIERRE PU", "ASPECT PIERRE NATURELLE\nLÉGÈRE ET FACILE À POSER"),
        "flexible_pu_stone": _copy("PIERRE FLEXIBLE PU", "ASPECT PIERRE NATURELLE\nFLEXIBLE POUR MURS COURBES"),
        "plastic_wood_flooring": _copy("REVÊTEMENT BOIS PLASTIQUE", "ASPECT BOIS NATUREL\nIMPERMÉABLE ET FACILE À ENTRETENIR"),
        "crystal_rock_wall": _copy("PANNEAU MURAL CRYSTAL ROCK", "FINITION PIERRE HAUT DE GAMME\nIMPERMÉABLE ET FACILE À ENTRETENIR"),
    },
    "it": {
        "white_panel": _copy("PANNELLO DECORATIVO BIANCO", "EFFETTO PIASTRELLA PULITO\nINSTALLAZIONE FACILE"),
        "carbon_crystal": _copy("PANNELLO CARBON CRYSTAL", "EFFETTO MARMO PREMIUM\nINSTALLAZIONE PIÙ FACILE"),
        "metal_panel": _copy("PANNELLO METALLICO", "FINITURA METALLO SPAZZOLATO\nSTILE MODERNO PREMIUM"),
        "mirror_panel": _copy("PANNELLO SPECCHIO", "EFFETTO SPECCHIO LUMINOSO\nPIÙ SICURO NEGLI INTERNI"),
        "wood_veneer": _copy("PANNELLO EFFETTO LEGNO", "ASPETTO LEGNO NATURALE\nFINITURA CALDA PREMIUM"),
        "pu_stone": _copy("PIETRA PU", "ASPETTO PIETRA REALE\nLEGGERA E FACILE DA INSTALLARE"),
        "flexible_pu_stone": _copy("PIETRA FLESSIBILE PU", "ASPETTO PIETRA REALE\nFLESSIBILE PER PARETI CURVE"),
        "plastic_wood_flooring": _copy("PAVIMENTO IN LEGNO PLASTICO", "ASPETTO LEGNO NATURALE\nIMPERMEABILE E FACILE DA CURARE"),
        "crystal_rock_wall": _copy("PANNELLO PARETE CRYSTAL ROCK", "FINITURA PIETRA PREMIUM\nIMPERMEABILE E FACILE DA CURARE"),
    },
    "ru": {
        "white_panel": _copy("БЕЛАЯ ДЕКОРАТИВНАЯ ПАНЕЛЬ", "ЧИСТЫЙ ЭФФЕКТ ПЛИТКИ\nПРОСТОЙ МОНТАЖ"),
        "carbon_crystal": _copy("ПАНЕЛЬ CARBON CRYSTAL", "ПРЕМИАЛЬНЫЙ ЭФФЕКТ МРАМОРА\nПРОСТОЙ МОНТАЖ"),
        "metal_panel": _copy("МЕТАЛЛИЧЕСКАЯ ПАНЕЛЬ", "ЭФФЕКТ ШЛИФОВАННОГО МЕТАЛЛА\nСОВРЕМЕННЫЙ ПРЕМИАЛЬНЫЙ СТИЛЬ"),
        "mirror_panel": _copy("ЗЕРКАЛЬНАЯ ПАНЕЛЬ", "ЯРКИЙ ЗЕРКАЛЬНЫЙ ЭФФЕКТ\nБЕЗОПАСНЕЕ ДЛЯ ИНТЕРЬЕРА"),
        "wood_veneer": _copy("ПАНЕЛЬ ПОД ДЕРЕВО", "ЭФФЕКТ НАТУРАЛЬНОГО ДЕРЕВА\nТЁПЛАЯ ПРЕМИАЛЬНАЯ ОТДЕЛКА"),
        "pu_stone": _copy("PU-КАМЕНЬ", "ЭФФЕКТ НАТУРАЛЬНОГО КАМНЯ\nЛЁГКИЙ И ПРОСТОЙ В МОНТАЖЕ"),
        "flexible_pu_stone": _copy("ГИБКИЙ PU-КАМЕНЬ", "ЭФФЕКТ НАТУРАЛЬНОГО КАМНЯ\nДЛЯ ИЗОГНУТЫХ СТЕН"),
        "plastic_wood_flooring": _copy("ДРЕВЕСНО-ПОЛИМЕРНОЕ ПОКРЫТИЕ", "ЭФФЕКТ НАТУРАЛЬНОГО ДЕРЕВА\nВОДОСТОЙКОЕ И ПРОСТОЕ В УХОДЕ"),
        "crystal_rock_wall": _copy("СТЕНОВАЯ ПАНЕЛЬ CRYSTAL ROCK", "ПРЕМИАЛЬНЫЙ ЭФФЕКТ КАМНЯ\nВОДОСТОЙКАЯ И ПРОСТАЯ В УХОДЕ"),
    },
    "id": {
        "white_panel": _copy("PAPAN DEKORATIF PUTIH", "TAMPILAN UBIN YANG BERSIH\nMUDAH DIPASANG"),
        "carbon_crystal": _copy("PANEL CARBON CRYSTAL", "TAMPILAN MARMER PREMIUM\nLEBIH MUDAH DIPASANG"),
        "metal_panel": _copy("PANEL METAL", "FINISH LOGAM SIKAT\nGAYA MODERN PREMIUM"),
        "mirror_panel": _copy("PANEL CERMIN", "EFEK CERMIN YANG CERAH\nLEBIH AMAN UNTUK INTERIOR"),
        "wood_veneer": _copy("PANEL VENEER KAYU", "TAMPILAN KAYU ALAMI\nFINISH HANGAT PREMIUM"),
        "pu_stone": _copy("PU STONE", "TAMPILAN BATU ALAMI\nRINGAN DAN MUDAH DIPASANG"),
        "flexible_pu_stone": _copy("PU FLEXIBLE STONE", "TAMPILAN BATU ALAMI\nFLEKSIBEL UNTUK DINDING MELENGKUNG"),
        "plastic_wood_flooring": _copy("LANTAI KAYU PLASTIK", "TAMPILAN KAYU ALAMI\nTAHAN AIR DAN MUDAH DIRAWAT"),
        "crystal_rock_wall": _copy("PANEL DINDING CRYSTAL ROCK", "TAMPILAN BATU PREMIUM\nTAHAN AIR DAN MUDAH DIRAWAT"),
    },
    "tr": {
        "white_panel": _copy("BEYAZ DEKORATİF PANEL", "TEMİZ SERAMİK GÖRÜNÜMÜ\nKOLAY MONTAJ"),
        "carbon_crystal": _copy("CARBON CRYSTAL PANEL", "PREMİUM MERMER GÖRÜNÜMÜ\nDAHA KOLAY MONTAJ"),
        "metal_panel": _copy("METAL PANEL", "FIRÇALANMIŞ METAL YÜZEY\nMODERN PREMİUM STİL"),
        "mirror_panel": _copy("AYNA PANEL", "PARLAK AYNA ETKİSİ\nİÇ MEKÂNDA DAHA GÜVENLİ"),
        "wood_veneer": _copy("AHŞAP KAPLAMA PANEL", "DOĞAL AHŞAP GÖRÜNÜMÜ\nSICAK PREMİUM BİTİŞ"),
        "pu_stone": _copy("PU TAŞ", "GERÇEK TAŞ GÖRÜNÜMÜ\nHAFİF VE KOLAY MONTAJ"),
        "flexible_pu_stone": _copy("ESNEK PU TAŞ", "GERÇEK TAŞ GÖRÜNÜMÜ\nKAVİSLİ DUVARLAR İÇİN ESNEK"),
        "plastic_wood_flooring": _copy("PLASTİK AHŞAP ZEMİN", "DOĞAL AHŞAP GÖRÜNÜMÜ\nSU GEÇİRMEZ, KOLAY BAKIM"),
        "crystal_rock_wall": _copy("CRYSTAL ROCK DUVAR PANELİ", "PREMİUM TAŞ GÖRÜNÜMÜ\nSU GEÇİRMEZ, KOLAY BAKIM"),
    },
    # Korean is intentionally enabled only by the true-Huang task contract.
    # The cover copy remains available here so a validated `ko` handoff never
    # silently falls back to English at the final rendering step.
    "ko": {
        "white_panel": _copy("화이트 장식 패널", "깔끔한 타일 느낌\n간편한 시공"),
        "carbon_crystal": _copy("카본 크리스탈 패널", "프리미엄 대리석 느낌\n더 쉬운 시공"),
        "metal_panel": _copy("메탈 패널", "브러시드 메탈 마감\n모던 프리미엄 스타일"),
        "mirror_panel": _copy("미러 패널", "밝은 거울 효과\n실내에 더 안전하게"),
        "wood_veneer": _copy("우드 베니어 패널", "자연스러운 우드 느낌\n따뜻한 프리미엄 마감"),
        "pu_stone": _copy("PU 스톤", "리얼 스톤 느낌\n가볍고 시공이 간편"),
        "flexible_pu_stone": _copy("유연한 PU 스톤", "리얼 스톤 느낌\n곡면 벽에도 유연하게"),
    },
}

def localized_copy(material_id, language_code):
    language = LOCALIZED_COVER_COPY.get(language_code) or LOCALIZED_COVER_COPY["en"]
    return dict(language.get(material_id) or LOCALIZED_COVER_COPY["en"][material_id])
