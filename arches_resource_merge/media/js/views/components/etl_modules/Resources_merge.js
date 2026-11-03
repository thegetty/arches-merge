import ko from "knockout";
import { ViewModel } from "./arches-resource-merge";
import template from "templates/views/components/etl_modules/arches-resource-merge.htm";

// Existing registrations can still load this path before the naming migration.
if (!ko.components.isRegistered("Resources_merge")) {
    ko.components.register("Resources_merge", { viewModel: ViewModel, template });
}