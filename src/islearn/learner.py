import functools
import io
import itertools
import logging
import os.path
import pkgutil
from abc import ABC
from typing import List, Tuple, Set, Dict, Optional, cast, Callable, Iterable, Sequence

import isla.fuzzer
import z3
from grammar_graph import gg
from isla import language, isla_predicates
from isla.evaluator import evaluate
from isla.existential_helpers import paths_between
from isla.helpers import is_z3_var, z3_subst, dict_of_lists_to_list_of_dicts, is_nonterminal
from isla.isla_predicates import reachable
from isla.language import set_smt_auto_eval, ISLaUnparser
from isla.solver import ISLaSolver
from isla.type_defs import Grammar
from orderedset import OrderedSet
from pathos import multiprocessing as pmp

from islearn.helpers import parallel_all, connected_chains, non_consecutive_ordered_sub_sequences, \
    replace_formula_by_formulas, transitive_closure
from islearn.language import NonterminalPlaceholderVariable, PlaceholderVariable, NonterminalStringPlaceholderVariable, \
    parse_abstract_isla, StringPlaceholderVariable, AbstractISLaUnparser
from islearn.mutation import MutationFuzzer

STANDARD_PATTERNS_REPO = "patterns.yaml"
logger = logging.getLogger("learner")


def learn_invariants(
        grammar: Grammar,
        prop: Callable[[language.DerivationTree], bool],
        positive_examples: Optional[Iterable[language.DerivationTree]] = None,
        negative_examples: Optional[Iterable[language.DerivationTree]] = None,
        patterns: Optional[List[language.Formula | str]] = None,
        pattern_file: Optional[str] = None,
        activated_patterns: Optional[Iterable[str]] = None,
        deactivated_patterns: Optional[Iterable[str]] = None,
        k: int = 3) -> Dict[language.Formula, float]:
    positive_examples = set(positive_examples or [])
    negative_examples = set(negative_examples or [])

    assert all(prop(example) for example in positive_examples)
    assert all(not prop(example) for example in negative_examples)

    assert not activated_patterns or not deactivated_patterns
    if not patterns:
        pattern_repo = patterns_from_file(pattern_file or STANDARD_PATTERNS_REPO)
        if activated_patterns:
            patterns = [pattern for name in activated_patterns for pattern in pattern_repo[name]]
        else:
            patterns = list(pattern_repo.get_all(but=deactivated_patterns or []))
    else:
        patterns = [
            pattern if isinstance(pattern, language.Formula)
            else parse_abstract_isla(pattern, grammar)
            for pattern in patterns]

    # Also consider inverted patterns?

    logger.info(
        "Starting with %d positive, and %d negative samples.",
        len(positive_examples),
        len(negative_examples)
    )

    ne_before = len(negative_examples)
    pe_before = len(positive_examples)
    generate_sample_inputs(grammar, prop, negative_examples, positive_examples)

    logger.info(
        "Generated %d additional positive, and %d additional negative samples (by grammar fuzzing).",
        len(positive_examples) - pe_before,
        len(negative_examples) - ne_before
    )

    assert len(positive_examples) > 0, "Cannot learn without any positive examples!"

    pe_before = len(positive_examples)
    ne_before = len(negative_examples)
    mutation_fuzzer = MutationFuzzer(grammar, positive_examples, prop, k=k)
    for inp in mutation_fuzzer.run(num_iterations=50, alpha=.1, yield_negative=True):
        if prop(inp):
            positive_examples.add(inp)
        else:
            negative_examples.add(inp)

    logger.info(
        "Generated %d additional positive, and %d additional negative samples (by mutation fuzzing).",
        len(positive_examples) - pe_before,
        len(negative_examples) - ne_before
    )

    graph = gg.GrammarGraph.from_grammar(grammar)
    positive_examples = filter_inputs_by_paths(positive_examples, graph, max_cnt=10, k=k, prefer_small=True)
    positive_examples_for_learning = filter_inputs_by_paths(positive_examples, graph, max_cnt=7, k=k, prefer_small=True)
    negative_examples = filter_inputs_by_paths(negative_examples, graph, max_cnt=10, k=k, prefer_small=True)

    # logger.debug(
    #     "Examples for learning:\n%s",
    #     "\n".join(map(str, positive_examples_for_learning)))

    logger.info(
        "Reduced positive / negative samples to subsets of %d / %d samples based on k-path coverage, "
        "keeping %d positive examples for candidate generation.",
        len(positive_examples),
        len(negative_examples),
        len(positive_examples_for_learning),
    )

    candidates = generate_candidates(patterns, positive_examples_for_learning, grammar)
    logger.info("Found %d invariant candidates.", len(candidates))

    logger.info("Filtering invariants.")

    # Only consider *real* invariants
    invariants = [
        candidate for candidate in candidates
        if parallel_all(lambda inp: evaluate(candidate, inp, grammar).is_true(), positive_examples)
    ]

    logger.info("%d invariants remain after filtering.", len(invariants))

    # ne_before = len(negative_examples)
    # negative_examples.update(generate_counter_examples_from_formulas(grammar, prop, invariants))
    # logger.info(
    #     "Generated %d additional negative samples (from invariants).",
    #     len(negative_examples) - ne_before
    # )

    logger.info("Calculating precision.")

    with pmp.ProcessingPool(processes=2 * pmp.cpu_count()) as pool:
        eval_results = pool.map(
            lambda t: (t[0], int(evaluate(t[0], t[1], grammar).is_true())),
            itertools.product(invariants, negative_examples),
            chunksize=10
        )

    result: Dict[language.Formula, float] = {
        inv: 1 - (sum([eval_result for other_inv, eval_result in eval_results if other_inv == inv])
                  / len(negative_examples))
        for inv in invariants
    }

    logger.info("Found %d invariants with non-zero precision.", len([p for p in result.values() if p > 0]))

    # result: Dict[language.Formula, float] = {}
    # for invariant in invariants:
    #   with pmp.ProcessingPool(processes=pmp.cpu_count()) as pool:
    #       num_counter_examples = sum(pool.map(
    #           lambda negative_example: int(evaluate(invariant, negative_example, grammar).is_true()),
    #           negative_examples))

    #   result[invariant] = 1 - (num_counter_examples / (len(negative_examples) or 1))

    return dict(cast(List[Tuple[language.Formula, float]],
                     sorted(result.items(), key=lambda p: p[1], reverse=True)))


def generate_sample_inputs(
        grammar: Grammar,
        prop: Callable[[language.DerivationTree], bool],
        negative_examples: Set[language.DerivationTree],
        positive_examples: Set[language.DerivationTree],
        desired_number_examples: int = 10,
        num_tries: int = 100) -> None:
    fuzzer = isla.fuzzer.GrammarCoverageFuzzer(grammar)
    if len(positive_examples) < desired_number_examples or len(negative_examples) < desired_number_examples:
        i = 0
        while ((len(positive_examples) < desired_number_examples
                or len(negative_examples) < desired_number_examples)
               and i < num_tries):
            i += 1

            inp = fuzzer.expand_tree(language.DerivationTree("<start>", None))

            if prop(inp):
                positive_examples.add(inp)
            else:
                negative_examples.add(inp)


def generate_counter_examples_from_formulas(
        grammar: Grammar,
        prop: Callable[[language.DerivationTree], bool],
        formulas: Iterable[language.Formula],
        desired_number_counter_examples: int = 50,
        num_tries: int = 100) -> Set[language.DerivationTree]:
    result: Set[language.DerivationTree] = set()

    solvers = {
        formula: ISLaSolver(grammar, formula, enforce_unique_trees_in_queue=False).solve()
        for formula in formulas}

    i = 0
    while len(result) < desired_number_counter_examples and i < num_tries:
        # with pmp.ProcessingPool(processes=pmp.cpu_count()) as pool:
        #     result.update({
        #         inp
        #         for inp in e_assert_present(pool.map(lambda f: next(solvers[f]), formulas))
        #         if not prop(inp)
        #     })
        for formula in formulas:
            for _ in range(desired_number_counter_examples):
                inp = next(solvers[formula])
                if not prop(inp):
                    result.add(inp)

        i += 1

    return result


def filter_inputs_by_paths(
        inputs: Iterable[language.DerivationTree],
        graph: gg.GrammarGraph,
        max_cnt: int = 10,
        k: int = 3,
        prefer_small=False) -> Set[language.DerivationTree]:
    inputs = set(inputs)

    if len(inputs) <= max_cnt:
        return inputs

    tree_paths = {inp: graph.k_paths_in_tree(inp.to_parse_tree(), k) for inp in inputs}

    result: Set[language.DerivationTree] = set([])
    covered_paths: Set[Tuple[gg.Node, ...]] = set([])

    def uncovered_paths(inp: language.DerivationTree) -> Set[Tuple[gg.Node, ...]]:
        return {path for path in tree_paths[inp] if path not in covered_paths}

    while inputs and len(result) < max_cnt and len(covered_paths) < len(graph.k_paths(k)):
        if prefer_small:
            try:
                inp = next(inp for inp in sorted(inputs, key=lambda inp: len(inp))
                           if uncovered_paths(inp))
            except StopIteration:
                break
        else:
            inp = sorted(inputs, key=lambda inp: (len(uncovered_paths(inp)), -len(inp)), reverse=True)[0]

        covered_paths.update(tree_paths[inp])
        inputs.remove(inp)
        result.add(inp)

    return result


def generate_candidates(
        patterns: Iterable[language.Formula | str],
        inputs: Iterable[language.DerivationTree],
        grammar: Grammar) -> Set[language.Formula]:
    graph = gg.GrammarGraph.from_grammar(grammar)

    nonterminal_paths: Set[Tuple[str, ...]] = {
        tuple([inp.get_subtree(path[:idx]).value
               for idx in range(len(path))])
        for inp in inputs
        for path, _ in inp.leaves()
    }

    input_reachability_relation: Set[Tuple[str, str]] = transitive_closure({
        pair
        for path in nonterminal_paths
        for pair in [(path[i], path[k]) for i in range(len(path)) for k in range(i + 1, len(path))]
    })

    logger.debug("Computed input reachability relation of size %d", len(input_reachability_relation))

    filters: List[PatternInstantiationFilter] = [
        StructuralPredicatesFilter(),
        VariablesEqualFilter(),
        NonterminalStringInCountPredicatesFilter(graph, input_reachability_relation)
    ]

    patterns = [
        parse_abstract_isla(pattern, grammar) if isinstance(pattern, str)
        else pattern
        for pattern in patterns]

    result: Set[language.Formula] = set([])

    for pattern in patterns:
        logger.debug("Instantiating pattern\n%s", AbstractISLaUnparser(pattern).unparse())
        set_smt_auto_eval(pattern, False)

        # Instantiate various placeholder variables:
        # 1. Nonterminal placeholders
        pattern_insts_without_nonterminal_placeholders = instantiate_nonterminal_placeholders(
            pattern, input_reachability_relation, graph)

        logger.debug("Found %d instantiations of pattern meeting quantifier requirements",
                     len(pattern_insts_without_nonterminal_placeholders))

        # 2. Nonterminal-String placeholders
        pattern_insts_without_nonterminal_string_placeholders = instantiate_nonterminal_string_placeholders(
            grammar, pattern_insts_without_nonterminal_placeholders)

        logger.debug("Found %d instantiations of pattern after instantiating nonterminal string placeholders",
                     len(pattern_insts_without_nonterminal_string_placeholders))

        # 3. String placeholders
        pattern_insts_without_string_placeholders = instantiate_string_placeholders(
            pattern_insts_without_nonterminal_string_placeholders, inputs, grammar)

        logger.debug("Found %d instantiations of pattern after instantiating string placeholders",
                     len(pattern_insts_without_string_placeholders))

        assert all(not get_placeholders(candidate)
                   for candidate in pattern_insts_without_string_placeholders)

        pattern_insts_meeting_atom_requirements: Set[language.Formula] = \
            set(pattern_insts_without_string_placeholders)

        for pattern_filter in filters:
            pattern_insts_meeting_atom_requirements = {
                pattern_inst for pattern_inst in pattern_insts_meeting_atom_requirements
                if pattern_filter.predicate(pattern_inst, inputs)
            }

            logger.debug("%d instantiations remaining after filter '%s'",
                         len(pattern_insts_meeting_atom_requirements),
                         pattern_filter.name)

        result.update(pattern_insts_meeting_atom_requirements)

    return result


def instantiate_nonterminal_placeholders(
        pattern: language.Formula,
        input_reachability_relation: Set[Tuple[str, str]],
        graph: gg.GrammarGraph) -> Set[language.Formula]:
    in_visitor = InVisitor()
    pattern.accept(in_visitor)
    variable_chains: Set[Tuple[language.Variable, ...]] = connected_chains(in_visitor.result)
    assert all(chain[-1] == extract_top_level_constant(pattern) for chain in variable_chains)

    instantiations: List[Dict[NonterminalPlaceholderVariable, language.BoundVariable]] = []
    for variable_chain in variable_chains:
        nonterminal_sequences: Set[Tuple[str, ...]] = set([])

        partial_sequences: Set[Tuple[str, ...]] = {("<start>",)}
        while partial_sequences:
            partial_sequence = next(iter(partial_sequences))
            partial_sequences.remove(partial_sequence)
            if len(partial_sequence) == len(variable_chain):
                nonterminal_sequences.add(tuple(reversed(partial_sequence)))
                continue

            partial_sequences.update({
                partial_sequence + (to_nonterminal,)
                for (from_nonterminal, to_nonterminal) in input_reachability_relation
                if from_nonterminal == partial_sequence[-1]})

        nonredundant_nonterminal_sequences = {
            seq for seq in nonterminal_sequences
            if not any(
                other_seq != seq and
                chain_implies(tuple(reversed(other_seq)), tuple(reversed(seq)), graph)
                for other_seq in nonterminal_sequences
            )
        }

        new_instantiations = [
            {variable: language.BoundVariable(variable.name, nonterminal_sequence[idx])
             for idx, variable in enumerate(variable_chain[:-1])}
            for nonterminal_sequence in nonterminal_sequences]

        if not instantiations:
            instantiations = new_instantiations
        else:
            instantiations = [
                cast(Dict[NonterminalPlaceholderVariable, language.BoundVariable],
                     functools.reduce(dict.__or__, t))
                for t in list(itertools.product(instantiations, new_instantiations))
            ]

    result: Set[language.Formula] = {
        pattern.substitute_variables(instantiation)
        for instantiation in instantiations
    }

    return result

    # nonredundant_result: Set[language.Formula] = {
    #     f for f in result
    #     if not any(other != f and formula_structurally_implies(other, f, graph) for other in result)
    # }
    #
    # return nonredundant_result


def instantiate_nonterminal_string_placeholders(
        grammar: Grammar,
        inst_patterns: Set[language.Formula]) -> Set[language.Formula]:
    if all(not isinstance(placeholder, NonterminalStringPlaceholderVariable)
           for inst_pattern in inst_patterns
           for placeholder in get_placeholders(inst_pattern)):
        return inst_patterns

    result: Set[language.Formula] = set([])
    for inst_pattern in inst_patterns:
        for nonterminal in grammar:
            def replace_placeholder_by_nonterminal_string(
                    subformula: language.Formula) -> language.Formula | bool:
                if (not isinstance(subformula, language.SemanticPredicateFormula) and
                        not isinstance(subformula, language.StructuralPredicateFormula)):
                    return False

                if not any(isinstance(arg, NonterminalStringPlaceholderVariable) for arg in subformula.args):
                    return False

                new_args = [
                    nonterminal if isinstance(arg, NonterminalStringPlaceholderVariable)
                    else arg
                    for arg in subformula.args]

                constructor = (
                    language.StructuralPredicateFormula
                    if isinstance(inst_pattern, language.StructuralPredicateFormula)
                    else language.SemanticPredicateFormula)

                return constructor(subformula.predicate, *new_args)

            result.add(language.replace_formula(inst_pattern, replace_placeholder_by_nonterminal_string))

    return result


def instantiate_string_placeholders(
        inst_patterns: Set[language.Formula],
        inputs: Iterable[language.DerivationTree],
        grammar: Grammar,
) -> Set[language.Formula]:
    if all(not isinstance(placeholder, StringPlaceholderVariable)
           for inst_pattern in inst_patterns
           for placeholder in get_placeholders(inst_pattern)):
        return inst_patterns

    fragments: Dict[str, Set[str]] = {
        nonterminal: functools.reduce(
            set.intersection,
            [
                {
                    str(subtree)
                    for _, subtree in inp.filter(lambda t: t.value == nonterminal)
                    if str(subtree)
                }
                for inp in inputs
            ]
        )
        for nonterminal in grammar
    }

    result: Set[language.Formula] = set([])
    for inst_pattern in inst_patterns:
        def replace_placeholder_by_string(subformula: language.Formula) -> Optional[Iterable[language.Formula]]:
            if (not isinstance(subformula, language.SemanticPredicateFormula) and
                    not isinstance(subformula, language.SemanticPredicateFormula) and
                    not isinstance(subformula, language.SMTFormula)):
                return None

            if not any(isinstance(arg, StringPlaceholderVariable) for arg in subformula.free_variables()):
                return None

            non_ph_vars = {v for v in subformula.free_variables() if not isinstance(v, PlaceholderVariable)}

            insts = functools.reduce(set.__or__, [fragments.get(var.n_type, set([])) for var in non_ph_vars])

            ph_vars = {v for v in subformula.free_variables() if isinstance(v, StringPlaceholderVariable)}
            sub_result: Set[language.Formula] = {subformula}
            for ph in ph_vars:
                old_sub_result = set(sub_result)
                sub_result = set([])
                for f in old_sub_result:
                    for inst in insts:
                        def substitute_string_placeholder(formula: language.Formula) -> language.Formula | bool:
                            if not any(v == ph for v in formula.free_variables()
                                       if isinstance(v, StringPlaceholderVariable)):
                                return False

                            if isinstance(formula, language.SMTFormula):
                                return language.SMTFormula(cast(z3.BoolRef, z3_subst(formula.formula, {
                                    ph.to_smt(): z3.StringVal(inst)
                                })), *[v for v in formula.free_variables() if v != ph])
                            elif isinstance(formula, language.StructuralPredicateFormula):
                                return language.StructuralPredicateFormula(
                                    formula.predicate,
                                    *[inst if arg == ph else arg for arg in formula.args]
                                )
                            elif isinstance(formula, language.SemanticPredicateFormula):
                                return language.SemanticPredicateFormula(
                                    formula.predicate,
                                    *[inst if arg == ph else arg for arg in formula.args]
                                )

                            return False

                        sub_result.add(language.replace_formula(f, substitute_string_placeholder))

            return sub_result

        result.update(replace_formula_by_formulas(inst_pattern, replace_placeholder_by_string))

    return result


def formula_structurally_implies(
        formula_1: language.Formula, formula_2: language.Formula, graph: gg.GrammarGraph) -> bool:
    """
    This predicate holds true if both formula_1 and formula_2 are quantified formulas of the same structure
    starting with a quantifier block, and the elements addressed by the second quantifier are a subset of
    the elements addressed by the first quantifier.
    """

    qfr_block_1 = get_quantifier_block(formula_1)
    qfr_block_2 = get_quantifier_block(formula_2)

    if (len(qfr_block_1) != len(qfr_block_2) or
            any(not isinstance(qfr_block_1[idx], type(qfr_block_2[idx])) for idx in range(len(qfr_block_1)))):
        return False

    unconnected_chains: Tuple[List[Tuple[str, str]], List[Tuple[str, str]]] = ([], [])
    for idx, qfr_block in enumerate([qfr_block_1, qfr_block_2]):
        for qfr in qfr_block:
            unconnected_chains[idx].append((qfr.in_variable.n_type, qfr.bound_variable.n_type))

        unconnected_chains[idx].append((qfr.bound_variable.n_type, qfr.bound_variable.n_type))

        if not all(unconnected_chains[idx][c_idx][-1] == unconnected_chains[idx][c_idx + 1][0]
                   for c_idx in range(len(unconnected_chains[idx]) - 1)):
            return False

    chain_1 = tuple([chain[0] for chain in unconnected_chains[0]])
    chain_2 = tuple([chain[0] for chain in unconnected_chains[1]])

    return chain_implies(chain_1, chain_2, graph)


def get_quantifier_block(formula: language.Formula) -> List[language.QuantifiedFormula]:
    if isinstance(formula, language.QuantifiedFormula):
        return [formula] + get_quantifier_block(formula.inner_formula)

    if isinstance(formula, language.NumericQuantifiedFormula):
        return get_quantifier_block(formula.inner_formula)

    return []


def chain_implies(chain_1: Tuple[str, ...], chain_2: Tuple[str, ...], graph: gg.GrammarGraph) -> bool:
    if len(chain_1) != len(chain_2):
        return False

    if chain_1[-1] != chain_2[-1]:
        return False

    chain_1_closure = nonterminal_chain_closure(chain_1, graph)
    chain_2_closure = nonterminal_chain_closure(chain_2, graph)

    for cl_chain_2 in chain_2_closure:
        success = False
        for cl_chain_1 in chain_1_closure:
            if len(cl_chain_1) < len(cl_chain_2):
                continue

            if cl_chain_1 == cl_chain_2:
                success = True
                break

            for idx in range(len(cl_chain_1) - len(cl_chain_2) + 1):
                if cl_chain_1[idx:len(cl_chain_2) + idx] == cl_chain_2:
                    success = True
                    break

            if success:
                break

        if not success:
            # print(f"Failure: {cl_chain_2}")
            return False

    return True


def nonterminal_chain_closure(
        chain: Tuple[str, ...],
        graph: gg.GrammarGraph,
        max_num_cycles=0) -> Set[Tuple[str, ...]]:
    closure: Set[Tuple[str, ...]] = {(chain[0],)}
    for chain_elem in chain[1:]:
        old_chain_1_closure = set(closure)
        closure = set([])
        for partial_chain in old_chain_1_closure:
            closure.update({
                partial_chain + path[1:]
                for path in paths_between(graph, partial_chain[-1], chain_elem)})

    return closure


class PatternInstantiationFilter(ABC):
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, PatternInstantiationFilter) and self.name == other.name

    def __hash__(self):
        return hash(self.name)

    def predicate(self, formula: language.Formula, inputs: Iterable[language.DerivationTree]) -> bool:
        raise NotImplementedError()


class VariablesEqualFilter(PatternInstantiationFilter):
    def __init__(self):
        super().__init__("Variable Equality Filter")

    def predicate(self, formula: language.Formula, inputs: Iterable[language.DerivationTree]) -> bool:
        # We approximate satisfaction of constraints "var1 == var2" by checking whether there is
        # at least one input with two equal subtrees of nonterminal types matching those of the
        # variables.
        smt_equality_formulas: List[language.SMTFormula] = cast(
            List[language.SMTFormula],
            language.FilterVisitor(
                lambda f: (isinstance(f, language.SMTFormula) and
                           z3.is_eq(f.formula) and
                           len(f.free_variables()) == 2 and
                           all(is_z3_var(child) for child in f.formula.children()))
            ).collect(formula))

        if not smt_equality_formulas:
            return True

        for inp in inputs:
            success = True

            for smt_equality_formula in smt_equality_formulas:
                free_vars: List[NonterminalPlaceholderVariable] = cast(
                    List[NonterminalPlaceholderVariable],
                    list(smt_equality_formula.free_variables()))

                trees_1 = inp.filter(lambda t: t.value == free_vars[0].n_type)
                trees_2 = inp.filter(lambda t: t.value == free_vars[1].n_type)

                if not any(
                        t1.value == free_vars[0].n_type and
                        t2.value == free_vars[1].n_type
                        for (p1, t1), (p2, t2)
                        in itertools.product(trees_1, trees_2)
                        if str(t1) == str(t2) and p1 != p2):
                    success = False
                    break

            if success:
                return True

        return False


class StructuralPredicatesFilter(PatternInstantiationFilter):
    def __init__(self):
        super().__init__("Structural Predicates Filter")

    def predicate(self, formula: language.Formula, inputs: Iterable[language.DerivationTree]) -> bool:
        # We approximate satisfaction of structural predicate formulas by searching
        # inputs for subtrees with the right nonterminal types according to the argument
        # types of the structural formulas.

        structural_formulas: List[language.StructuralPredicateFormula] = cast(
            List[language.StructuralPredicateFormula],
            language.FilterVisitor(
                lambda f: isinstance(f, language.StructuralPredicateFormula)).collect(formula))

        if not structural_formulas:
            return True

        for inp in inputs:
            success = True
            for structural_formula in structural_formulas:
                arg_insts: Dict[language.Variable, Set[language.DerivationTree]] = {}
                for arg in structural_formula.free_variables():
                    arg_insts[arg] = {t for _, t in inp.filter(lambda subtree: subtree.value == arg.n_type)}
                arg_substitutions: List[Dict[language.Variable, language.DerivationTree]] = \
                    dict_of_lists_to_list_of_dicts(arg_insts)

                if not any(structural_formula.substitute_expressions(arg_substitution).evaluate(inp)
                           for arg_substitution in arg_substitutions):
                    success = False
                    break

            if success:
                return True

        return False


class NonterminalStringInCountPredicatesFilter(PatternInstantiationFilter):
    def __init__(self, graph: gg.GrammarGraph, input_reachability_relation: Set[Tuple[str, str]]):
        super().__init__("Nonterminal String in `count` Predicates Filter")
        self.graph = graph
        self.input_reachability_relation = input_reachability_relation

    def reachable(self, from_nonterminal: str, to_nonterminal: str) -> bool:
        return reachable(self.graph, from_nonterminal, to_nonterminal)

    def reachable_in_inputs(self, from_nonterminal: str, to_nonterminal: str) -> bool:
        return (from_nonterminal, to_nonterminal) in self.input_reachability_relation

    def predicate(self, formula: language.Formula, inputs: Iterable[language.DerivationTree]) -> bool:
        # In `count(elem, nonterminal, num)` occurrences
        # 1. the nonterminal must be reachable from the nonterminal type of elem. We
        #    consider reachability as defined by the sample inputs, not the grammar,
        #    to increase precision,
        # 2. it must be possible that the nonterminal occurs a variable number of times
        #    in the subgrammar of elem's nonterminal type.

        count_predicates: List[language.SemanticPredicateFormula] = cast(
            List[language.SemanticPredicateFormula],
            language.FilterVisitor(
                lambda f: isinstance(f, language.SemanticPredicateFormula) and
                          f.predicate == isla_predicates.COUNT_PREDICATE).collect(formula))

        if not count_predicates:
            return True

        def reachable_variable_number_of_times(start_nonterminal: str, needle_nonterminal: str) -> bool:
            return any(self.reachable_in_inputs(start_nonterminal, other_nonterminal) and
                       self.reachable(other_nonterminal, other_nonterminal) and
                       self.reachable_in_inputs(other_nonterminal, needle_nonterminal)
                       for other_nonterminal in self.graph.grammar)

        return any(
            reachable_variable_number_of_times(count_predicate.args[0].n_type, count_predicate.args[1])
            for count_predicate in count_predicates
        )


class InVisitor(language.FormulaVisitor):
    def __init__(self):
        self.result: Set[Tuple[language.Variable, language.Variable]] = set()

    def visit_exists_formula(self, formula: language.ExistsFormula):
        self.handle(formula)

    def visit_forall_formula(self, formula: language.ForallFormula):
        self.handle(formula)

    def handle(self, formula: language.QuantifiedFormula):
        self.result.add((formula.bound_variable, formula.in_variable))


def get_placeholders(formula: language.Formula) -> Set[PlaceholderVariable]:
    placeholders = {var for var in language.VariablesCollector.collect(formula)
                    if isinstance(var, PlaceholderVariable)}
    supported_placeholder_types = {
        NonterminalPlaceholderVariable,
        NonterminalStringPlaceholderVariable,
        StringPlaceholderVariable}

    assert all(any(isinstance(ph, t) for t in supported_placeholder_types) for ph in placeholders), \
        "Only " + ", ".join(map(lambda t: t.__name__, supported_placeholder_types)) + " supported so far."

    return placeholders


def extract_top_level_constant(candidate):
    return next(
        (c for c in language.VariablesCollector.collect(candidate)
         if isinstance(c, language.Constant) and not c.is_numeric()))


class PatternRepository:
    DEFAULT_GROUP = "default"

    def __init__(self, data: List[Dict[str, str]]):
        self.groups: Dict[str, Dict[str, language.Formula]] = {}
        for entry in data:
            name = entry["name"]
            group = entry.get("group", PatternRepository.DEFAULT_GROUP)
            constraint = parse_abstract_isla(entry["constraint"])
            self.groups.setdefault(group, {})[name] = constraint

    def __getitem__(self, item: str) -> Set[language.Formula]:
        if item in self.groups:
            return set(self.groups[item].values())

        for elements in self.groups.values():
            if item in elements:
                return {elements[item]}

        return set([])

    def __contains__(self, item: str) -> bool:
        return bool(self[item])

    def __len__(self):
        return sum([len(group.values()) for group in self.groups.values()])

    def get_all(self, but: Iterable[str] = tuple()) -> Set[language.Formula]:
        exclude: Set[language.Formula] = set([])
        for excluded in but:
            exclude.update(self[excluded])

        all_patterns: Set[language.Formula] = set(
            functools.reduce(set.__or__, [set(d.values()) for d in self.groups.values()]))

        return all_patterns.intersection(exclude)

    def __str__(self):
        result = ""
        for group in self.groups:
            for name in self.groups[group]:
                result += f"- name: {name}\n"
                if group != PatternRepository.DEFAULT_GROUP:
                    result += f"  group: {group}\n"
                result += "  constraint: |\n"
                constraint_str = AbstractISLaUnparser(self.groups[group][name]).unparse()
                constraint_str = "\n".join(["      " + line for line in constraint_str.split("\n")])
                result += constraint_str
                result += "\n\n"

        return result.strip()


def patterns_from_file(file_name: str = STANDARD_PATTERNS_REPO) -> PatternRepository:
    from yaml import load
    try:
        from yaml import CLoader as Loader
    except ImportError:
        from yaml import Loader

    if os.path.isfile(file_name):
        f = open(file_name, "r")
        contents = f.read()
        f.close()
    else:
        contents = pkgutil.get_data("islearn", STANDARD_PATTERNS_REPO).decode("UTF-8")

    data = load(io.StringIO(contents), Loader=Loader)
    assert isinstance(data, list)
    assert len(data) > 0
    assert all(isinstance(entry, dict) for entry in data)

    return PatternRepository(data)
