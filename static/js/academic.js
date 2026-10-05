/**
 * academic.js
 * Dependent dropdowns for an academic group:
 *   faculty -> program -> year of study / specialisation
 *
 * The page provides the structure as JSON (element #academic-data, see
 * the academic_fields macro) and four <select> elements. Choosing a
 * faculty lists its programs; choosing a program lists the years it
 * offers and its specialisations (hidden when it has none).
 *
 * This only makes valid choices easy - the server checks every
 * combination again when the form is submitted.
 */
(function () {
    const dataEl = document.getElementById("academic-data");
    if (!dataEl) return;

    const tree = JSON.parse(dataEl.textContent);
    const faculty = document.getElementById("faculty_id");
    const program = document.getElementById("program_id");
    const year = document.getElementById("study_year_id");
    const specialisation = document.getElementById("specialisation_id");
    const specialisationField = document.getElementById("specialisation-field");

    function fill(select, items, placeholder, selected) {
        select.replaceChildren(new Option(placeholder, ""));
        items.forEach((item) => select.add(new Option(item.name, item.id)));
        const wanted = String(selected || "");
        select.value = items.some((item) => String(item.id) === wanted) ? wanted : "";
        select.disabled = items.length === 0;
    }

    function currentFaculty() {
        return tree.find((f) => String(f.id) === faculty.value);
    }

    function currentProgram() {
        const f = currentFaculty();
        return f && f.programs.find((p) => String(p.id) === program.value);
    }

    function showProgramDetails(selectedYear, selectedSpecialisation) {
        const p = currentProgram();
        fill(year, p ? p.years : [], p ? "Choose a year" : "Choose a program first", selectedYear);
        fill(specialisation, p ? p.specialisations : [], "No specialisation", selectedSpecialisation);
        specialisationField.classList.toggle("hidden-block", !p || p.specialisations.length === 0);
    }

    function showPrograms(selectedProgram, selectedYear, selectedSpecialisation) {
        const f = currentFaculty();
        fill(program, f ? f.programs : [], f ? "Choose a program" : "Choose a faculty first", selectedProgram);
        showProgramDetails(selectedYear, selectedSpecialisation);
    }

    // ---- Initial state: restore what was already chosen / saved ----
    const savedProgram = program.dataset.selected;
    const owner = tree.find((f) => f.programs.some((p) => String(p.id) === savedProgram));
    fill(faculty, tree, "Choose a faculty", faculty.dataset.selected || (owner && owner.id));
    showPrograms(savedProgram, year.dataset.selected, specialisation.dataset.selected);

    faculty.addEventListener("change", () => showPrograms());
    program.addEventListener("change", () => showProgramDetails());
})();
