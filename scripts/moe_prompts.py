"""Domain-diverse LLM test prompts (operator, 27 September 2026: "versuche
verschiedene MoEs zu triggern, Biologie, Mathematik, Chemie, Sprache,
Geschichte ...").

Twenty authored synthetic prompts, one per domain, in eight languages. Each
asks for a long, structured expert answer, so the whole decode is real
domain content rather than filler. With 20 concurrent requests on different
domains a mixture-of-experts model routes each decode step's batch across a
wider set of experts. vLLM does not expose the routing without changing the
engine, so the test measures its effects (throughput per domain, GPU power,
clocks). No user content; completions are never stored.
"""
import random
import secrets

DOMAINS = {
    "biologie": (
        "Du bist Professorin für Molekularbiologie. Schreibe ein ausführliches Lehrbuchkapitel "
        "über die Regulation der Genexpression bei Eukaryoten. Behandle Chromatinstruktur und "
        "Histonmodifikationen (Acetylierung, Methylierung, H3K27me3, H3K4me3), DNA-Methylierung an "
        "CpG-Inseln, Transkriptionsfaktoren und Enhancer-Promotor-Schleifen, den Mediator-Komplex, "
        "alternatives Spleißen durch SR-Proteine und hnRNPs, microRNAs und den RISC-Komplex, "
        "Nonsense-mediated Decay sowie epigenetische Vererbung. Erkläre jeweils ein konkretes "
        "Experiment (ChIP-seq, ATAC-seq, RNA-seq, CRISPR-Interferenz), wie man es auswertet und "
        "welche Kontrollen nötig sind. Füge am Ende 15 Prüfungsfragen mit Musterlösungen hinzu."),
    "mathematik": (
        "Prove the following rigorously in LaTeX, step by step, stating every lemma you use: "
        "(1) There are infinitely many primes congruent to 3 mod 4. (2) The square root of 2 is "
        "irrational; generalise to the square root of any non-square integer. (3) Every continuous "
        "function on a closed bounded interval is uniformly continuous (Heine-Cantor). (4) The sum "
        "of 1/n^2 equals pi^2/6, via the Fourier series of f(x) = x on (-pi, pi) and Parseval. "
        "(5) Every finite group of prime order is cyclic. (6) Compute eigenvalues and eigenvectors "
        "of [[2,1,0],[1,2,1],[0,1,2]] and diagonalise it. (7) Solve a_n = 5a_{n-1} - 6a_{n-2}, "
        "a_0 = 1, a_1 = 4, and prove the closed form by induction. After each proof add a remark "
        "on a common student mistake."),
    "chemie": (
        "As an organic chemistry tutor, explain in full mechanistic detail, with arrow-pushing "
        "described in text, intermediates, stereochemistry and energetics: SN1 versus SN2 on "
        "2-bromobutane, E1 versus E2 with Zaitsev and Hofmann products, the aldol condensation of "
        "acetaldehyde, the Diels-Alder reaction of cyclopentadiene and maleic anhydride (endo "
        "rule), Grignard addition to a ketone, Fischer esterification, the Wittig reaction and the "
        "nitration of toluene with its ortho/para ratio. Then plan a multi-step synthesis of "
        "ibuprofen from isobutylbenzene with every reagent and condition. Give SMILES for every "
        "compound, balanced equations with molar masses, and the theoretical yield for 50.0 g of "
        "isobutylbenzene."),
    "physik": (
        "Derive step by step, with all intermediate equations in LaTeX: the time-independent "
        "Schroedinger equation of the hydrogen atom in spherical coordinates, separation of "
        "variables, the spherical harmonics, the radial equation and E_n = -13.6 eV / n^2; "
        "first-order perturbation theory for the Stark effect at n = 2; spin-orbit coupling and "
        "fine structure; the harmonic oscillator with ladder operators; and the uncertainty "
        "relation from the Cauchy-Schwarz inequality. Finish with worked numbers: the H-alpha "
        "wavelength, the zero-point energy of CO with k = 1902 N/m, and the tunnelling probability "
        "of a 0.5 eV electron through a 1 eV, 0.5 nm barrier."),
    "geschichte": (
        "Schreibe eine ausführliche, quellenkritische Darstellung der Habsburgermonarchie von 1526 "
        "bis 1918: die Türkenbelagerungen Wiens 1529 und 1683, der Dreißigjährige Krieg und der "
        "Westfälische Friede, Maria Theresia und die Reformen Josephs II., die Napoleonischen "
        "Kriege und der Wiener Kongress, die Revolution von 1848, der Ausgleich von 1867, die "
        "Nationalitätenfrage in Böhmen, Galizien und Ungarn, das Attentat von Sarajevo und der "
        "Zerfall 1918. Nenne zu jedem Abschnitt Jahreszahlen, Schlüsselpersonen, zeitgenössische "
        "Quellen und historiographische Kontroversen. Schließe mit einer Zeittafel von 60 "
        "Einträgen."),
    "linguistik": (
        "Compare the morphology and syntax of Finnish, Turkish, Japanese, Swahili, Georgian and "
        "Navajo. For each language give at least eight example sentences in the original script or "
        "standard orthography, with interlinear glosses following the Leipzig glossing rules and a "
        "free translation. Cover agglutination versus fusion, vowel harmony, case systems, "
        "ergativity, evidentiality, noun classes, polysynthesis and word order. Then give a "
        "comparative table and explain which features are areal and which genetic, naming the "
        "language families."),
    "franzoesisch": (
        "Rédige en français une dissertation littéraire complète (introduction, trois parties avec "
        "sous-parties, conclusion) sur le sujet : « Le roman du XIXe siècle est-il un miroir de la "
        "société ? ». Appuie-toi sur Balzac (La Comédie humaine), Stendhal (Le Rouge et le Noir), "
        "Flaubert (Madame Bovary), Zola (Les Rougon-Macquart) et Maupassant. Analyse le réalisme, "
        "le naturalisme, la focalisation, le style indirect libre et la critique sociale, en "
        "commentant précisément des passages que tu reformules. Termine par une ouverture sur le "
        "Nouveau Roman et une bibliographie commentée de quinze titres."),
    "japanisch": (
        "日本の四季と伝統文化について、詳しい解説文を日本語で書いてください。春の花見、夏の祭りと"
        "花火、秋の紅葉狩りと月見、冬の正月行事を、それぞれ歴史的背景、地域ごとの違い、関連する"
        "和歌や俳句（松尾芭蕉、与謝蕪村、小林一茶の句を例に挙げて解釈）とともに説明してください。"
        "さらに、季語の仕組み、茶道と華道における季節感、和菓子の意匠について述べ、最後に外国人"
        "向けの季節ごとの旅行プランを三つ提案してください。できるだけ長く、丁寧な文体で書いて"
        "ください。"),
    "python": (
        "Implement in Python 3.12, with type hints, docstrings and unittest tests: a red-black tree "
        "with insert, delete and in-order iteration; Dijkstra and A* on a grid with obstacles; a "
        "trie-based autocomplete ranked by frequency; Knuth-Morris-Pratt string search; an LRU cache "
        "with O(1) operations from a dict and a doubly linked list; and a shunting-yard parser and "
        "evaluator for arithmetic expressions with operator precedence. For each, explain the "
        "complexity, draw the data structure as ASCII art and list the edge cases the tests "
        "cover."),
    "rust": (
        "Write a production-quality lock-free multi-producer single-consumer queue in Rust with "
        "std::sync::atomic, commenting every memory ordering (Relaxed, Acquire, Release, AcqRel, "
        "SeqCst) and why it suffices. Then implement the same in C11 with stdatomic.h. Explain the "
        "ABA problem, hazard pointers versus epoch-based reclamation, false sharing and cache-line "
        "padding, and how to test it with loom and ThreadSanitizer. Include the benchmarks you "
        "would run and a comparison with Mutex<VecDeque>."),
    "sql": (
        "Design a PostgreSQL 16 schema for a multi-tenant hospital information system: patients, "
        "admissions, wards, beds, staff, shifts, prescriptions, lab results and billing. Give full "
        "DDL with constraints, indexes, partial indexes, row-level security policies per tenant and "
        "audit triggers. Then write 20 non-trivial queries (window functions, recursive CTEs, "
        "LATERAL joins, JSONB aggregation, GROUPING SETS) with explanations and expected plans, and "
        "discuss normalisation, isolation levels and avoiding deadlocks during bed assignment."),
    "medizin": (
        "Du bist Oberarzt für Innere Medizin. Erstelle eine ausführliche Fallbesprechung: Eine "
        "67-jährige Patientin kommt mit Dyspnoe, Beinödemen, Nykturie und NT-proBNP 4800 pg/ml; "
        "Vorerkrankungen Diabetes mellitus Typ 2, arterielle Hypertonie, CKD Stadium 3b. Gehe durch "
        "Anamnese, Untersuchung, Differentialdiagnosen, Diagnostik (EKG, Echokardiographie, Labor, "
        "Bildgebung), die Einteilung nach ESC-Leitlinie, die leitliniengerechte Therapie der "
        "Herzinsuffizienz (ARNI, Betablocker, MRA, SGLT2-Inhibitoren) mit Dosierungen, "
        "Nierenanpassung, Kontraindikationen und Interaktionen, sowie das Monitoring. Schließe mit "
        "einem Entlassbrief."),
    "recht": (
        "Erstelle ein juristisches Gutachten im Gutachtenstil (Obersatz, Definition, Subsumtion, "
        "Ergebnis): Ein österreichisches Start-up verarbeitet Gesundheitsdaten der Nutzer einer "
        "Fitness-App, überträgt sie an einen Cloud-Anbieter in den USA und trainiert damit ein "
        "KI-Modell. Ein Nutzer verlangt Auskunft, Löschung und Schadenersatz. Prüfe die "
        "Rechtmäßigkeit nach DSGVO (Art. 5, 6, 9, 13, 15, 17, 22, 44 ff., 82), das EU-US Data "
        "Privacy Framework, die Rolle der Datenschutzbehörde und die Anforderungen des AI Act an "
        "Hochrisiko-Systeme. Diskutiere einschlägige EuGH-Urteile (Schrems II, Österreichische "
        "Post) und gib eine Handlungsempfehlung."),
    "finanzen": (
        "Perform a complete discounted-cash-flow valuation of a fictional mid-cap industrial "
        "company: revenue 1.2 billion EUR, EBITDA margin 14 %, capex 5 % and working capital 12 % "
        "of revenue, tax rate 24 %, net debt 380 million EUR, 85 million shares. Build a 10-year "
        "forecast table with explicit growth assumptions, compute free cash flow to firm per year, "
        "derive WACC from CAPM (risk-free 2.6 %, beta 1.15, market risk premium 5.5 %) and a 4.8 % "
        "cost of debt, compute terminal value by Gordon growth and by exit multiple, and give a "
        "sensitivity table of enterprise value and value per share. Show every calculation."),
    "musik": (
        "Write a 16-bar four-part chorale in the style of J. S. Bach in C minor, in note names for "
        "soprano, alto, tenor and bass, then analyse it bar by bar: Roman numerals and figured bass "
        "for every chord, cadences, secondary dominants, the Neapolitan sixth, augmented sixth "
        "chords, suspensions and passing tones, and any voice-leading issues. Then explain species "
        "counterpoint (first to fifth species) with short examples, the circle of fifths, modal "
        "mixture, and how jazz reharmonisation (tritone substitution, ii-V-I, altered dominants) "
        "would transform the same melody."),
    "philosophie": (
        "Verfasse eine ausführliche philosophische Abhandlung über das Problem der Induktion: Humes "
        "Argument, Kants Antwort mit den synthetischen Urteilen a priori und den Kategorien, "
        "Poppers Falsifikationismus, Goodmans neues Rätsel der Induktion (grue), bayesianische "
        "Bestätigungstheorie und maschinelles Lernen als Induktion. Rekonstruiere jedes Argument in "
        "nummerierten Prämissen und Konklusion, prüfe Gültigkeit und Schlüssigkeit, und schreibe "
        "einen Dialog zwischen Hume, Kant und Popper über die Frage, ob ein neuronales Netz etwas "
        "weiß."),
    "astronomie": (
        "Explain stellar evolution from molecular cloud collapse to the end states: derive the "
        "Jeans criterion; protostars and the Hayashi track; the proton-proton chain and CNO cycle "
        "with reaction equations and energy per step; the mass-luminosity relation; red giant "
        "branch, helium flash, horizontal branch, AGB and thermal pulses; planetary nebulae, white "
        "dwarfs and the order of magnitude of the Chandrasekhar limit; core-collapse supernovae, "
        "neutron stars, pulsars and black holes. Work out the main-sequence lifetime of a 2 "
        "solar-mass star, the Schwarzschild radius of 10 solar masses, and the luminosity of "
        "Sirius A from its radius and temperature."),
    "geologie": (
        "Escribe en español un capítulo universitario detallado sobre la tectónica de placas y la "
        "formación de los Alpes y los Andes: la estructura interna de la Tierra, la deriva "
        "continental de Wegener, la expansión del fondo oceánico, los tipos de límites de placa, "
        "la subducción y su magmatismo, la orogénesis alpina (napas, flysch, molasa), la orogenia "
        "andina y los volcanes del Cinturón de Fuego, los terremotos y la magnitud de momento, y la "
        "geología de los yacimientos de cobre en Chile. Incluye diagramas descritos en texto, un "
        "glosario de 40 términos y 10 preguntas de examen con respuestas."),
    "lyrik": (
        "Schreibe einen Zyklus von zwölf Gedichten auf Deutsch über die zwölf Monate eines Jahres "
        "in den Alpen, jedes in einer anderen Form: Sonett nach Petrarca, Sonett nach Shakespeare, "
        "Ghasel, Terzinen, Stanze, Haiku-Kette, freie Rhythmen, Ballade mit Kreuzreim, Elegie in "
        "Distichen, Villanelle, Rondeau und Knittelvers. Gib vor jedem Gedicht Versmaß und "
        "Reimschema an und erkläre danach die verwendeten rhetorischen Figuren (Alliteration, "
        "Enjambement, Chiasmus, Anapher, Synästhesie)."),
    "mehrsprachig": (
        "请用中文详细解释《论语》中关于“仁”、“礼”、“孝”的十段原文（给出原文、现代汉语翻译和注释），"
        "并比较孔子与孟子、荀子的人性论。Затем на русском языке напиши подробное эссе о влиянии "
        "конфуцианства на политическую культуру Восточной Азии, сравни его с византийской традицией "
        "в России и приведи исторические примеры XIX и XX веков. وأخيرًا، اكتب باللغة العربية فقرة "
        "طويلة عن تاريخ بيت الحكمة في بغداد وحركة ترجمة الفلسفة اليونانية."),
}

NAMES = tuple(DOMAINS)


def moe_request_body(model, domain, max_tokens, *, temperature=0.7, nonce=None):
    """One streamed request. ``ignore_eos`` keeps the decode length fixed; the
    long-form task keeps it real content. A random opening defeats vLLM's
    prefix cache, so every request also runs a real prefill."""
    nonce = nonce or secrets.token_hex(8)
    return {"model": model, "stream": True, "max_tokens": max_tokens,
            "temperature": temperature, "top_p": 0.95, "ignore_eos": True,
            "messages": [{"role": "user", "content": f"[{nonce}] {DOMAINS[domain]} "
                          "Answer as thoroughly and at as much length as possible."}]}


def crisscross_request_body(model, max_tokens, *, rng=None, domains=5, temperature=0.7,
                            nonce=None):
    """Operator, 27 September 2026: "verschiedene Anfragen von verschiedenen MoEs …
    ein Kreuz und Quer, damit viele Multiplikationen gefahren werden müssen".
    One request carries ``domains`` random tasks (mixed languages) and must switch
    task after every paragraph, so the experts change within each sequence, and
    the longer prompt adds real prefill work."""
    rng = rng or random.Random()
    nonce = nonce or secrets.token_hex(8)
    picked = rng.sample(NAMES, domains)
    labels = [chr(ord("A") + i) for i in range(domains)]
    tasks = "\n\n".join(f"[{label}] ({name}) {DOMAINS[name]}"
                         for label, name in zip(labels, picked))
    order = ", ".join(labels)
    instruction = (f"[{nonce}] Bearbeite die folgenden {domains} Aufgaben kreuz und quer: "
                   f"Wechsle nach JEDEM Absatz zur nächsten Aufgabe ({order}, {labels[0]}, …) "
                   "und schreibe jeweils in der Sprache der Aufgabe weiter, bis alle Aufgaben "
                   "vollständig und ausführlich beantwortet sind. Beginne jeden Absatz mit der "
                   "Kennung der Aufgabe, z. B. [A].")
    return {"model": model, "stream": True, "max_tokens": max_tokens,
            "temperature": temperature, "top_p": 0.95, "ignore_eos": True,
            "messages": [{"role": "user", "content": instruction + "\n\n" + tasks}]}
