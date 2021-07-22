import sys
import json
import argparse
from covizu.clustering import consensus
from Bio import Phylo
from io import StringIO
from csv import DictReader


def parse_labels(handle):
    """
    Parse labels CSV - assumes header contains 'name' and 'index'
    :param handle:  open stream to CSV file in read mode
    :return:  dict, lists of genome labels keyed by tip index
    """
    rows = DictReader(handle)
    results = {}
    for row in rows:
        if row['index'] not in results:
            results.update({row['index']: []})
        results[row['index']].append(row['name'])
    return results


def get_parents(tree):
    """ generate dictionary of child->parent associations """
    parents = {}
    for clade in tree.find_clades(order='level'):
        for child in clade:
            parents.update({child: clade})
    return parents


def collapse_polytomies(tree, minlen=0.5):
    """
    Excise branches with zero branch lengths
    :param tree:  Bio.Phylo object
    :return:  Bio.Phylo
    """
    parents = get_parents(tree)

    # prune tips with zero branch length, use to label internal node
    for tip in tree.get_terminals():
        if tip.branch_length > minlen:
            continue
        parent = parents[tip]
        parent.clades.remove(tip)
        if parent.name is None:
            parent.name = tip.name
        else:
            parent.name = '|'.join([parent.name, tip.name])

    # remove internal branches
    for node in tree.get_nonterminals():
        if node.branch_length is None or node.branch_length > minlen or node == tree.root:
            continue
        parent = parents[node]
        parent.clades.remove(node)
        # transfer child clades to parent
        parent.clades.extend(node.clades)
        for child in node.clades:
            parents[child] = parent
        # transfer labels to parent
        if node.name:
            if parent.name is None:
                parent.name = node.name
            else:
                parent.name = '|'.join([parent.name, node.name])

    return tree


def print_phylo(tree):
    """ DEBUGGING - format() not implemented for Clade objects """
    with StringIO() as handle:
        Phylo.write(tree, handle, "newick")
        print(handle.getvalue())


def annotate_tree(tree, label_dict, minlen=0.5, callback=None):
    """
    Extract beadplot node and edge information from NJ trees and labels generated by
    clustering.py: build_trees().  Each row of the beadplot corresponds to a "variant"
    that comprises genomes with identical feature vectors (clustering.py: get_sym_diffs()),
    AND any tips in the consensus NJ tree that are separated by zero branch lengths.
    Use tips with zero branch lengths to label internal nodes.
    For internal nodes that remain unlabelled, use the closest tip in time.

    :param tree:  Phylo.Clade, consensus tree
    :param label_dict:  dict, lists of genome labels keyed by tree tip integer index
    :return:  dict, lists of sequence labels keyed by integer indices
    """

    # validate tree and labels
    tip_labels = set([tip.name for tip in tree.get_terminals()])
    set_diff = tip_labels.difference(set(label_dict.keys()))
    if len(set_diff) > 0 and callback:
        callback("mismatch detected between tree and label file:", level='ERROR')
        callback("tip_labels: {}\n".format(tip_labels), level='ERROR')
        callback("label_dict.keys(): {}\n".format(label_dict.keys()), level='ERROR')
        callback("set_diff: {}\n".format(set_diff), level='ERROR')

    tree = collapse_polytomies(tree, minlen=minlen)  # label internal nodes

    # update nodes with labels
    for tip in tree.get_terminals():
        if '|' in tip.name:
            # carry over labels for all tips
            tip.labels = []
            for tn in tip.name.split('|'):
                tip.labels.extend(label_dict[tn])
        else:
            tip.labels = label_dict[tip.name]

    for node in tree.get_nonterminals():
        node.labels = []
        if node.name is None:
            # unsampled internal node
            node.name = ''
        else:
            # sampled internal node
            for idx in node.name.split('|'):
                node.labels.extend(label_dict[idx])

    return tree


def serialize_tree(tree):
    """
    Convert annotated tree object to JSON
    TODO: label nodes with features (genetic differences)
    :param tree:  Phylo.BaseTree object from annotate_tree()
    :return:  dict, containing 'nodes' and 'edges'
    """
    obj = {'nodes': {}, 'edges': []}
    variant_d = {}
    parents = get_parents(tree)

    us_count = 0  # number of unsampled variants
    for node in tree.find_clades(order='level'):
        if node.labels:
            # sort samples by [_COLDATE_, country, region, accession, label]
            intermed = [label.split('|')[::-1] for label in node.labels]
            intermed.sort()  # ISO dates sort in increasing order
            variant = intermed[0][3]  # use accession of earliest sample to ID variant

            # populate list with samples
            obj['nodes'].update({variant: intermed})
        else:
            variant = 'unsampled'+str(us_count)
            obj['nodes'].update({variant: []})
            us_count += 1

        variant_d.update({node: variant})

        if node is tree.root:
            continue  # no edge
        parent = parents[node]
        # parent ID, child ID, branch length, node support
        obj['edges'].append([variant_d[parent], variant, round(node.branch_length, 2),
                             node.confidence])

    return obj


def parse_args():
    """ Command line interface """
    parser = argparse.ArgumentParser(description="")
    parser.add_argument("tree", type=argparse.FileType('r'),
                        help="input, path to file with consensus tree or bootstrap trees")
    parser.add_argument("labels", type=argparse.FileType('r'),
                        help="input, path to file with sequence label to tip index map")
    parser.add_argument("-o", "--outfile", type=argparse.FileType('w'), default='-',
                        help="output, path to file to write JSON, defaults to stdout")
    parser.add_argument("--boot", action="store_true",
                        help="option, indicates that input file contains bootstrap trees")
    parser.add_argument("--cutoff", type=float, default=0.5,
                        help="option, if user sets --boot, specifies bootstrap support "
                             "threshold parameter (default 0.5)")
    parser.add_argument("--minlen", type=float, default=0.5,
                        help="option, minimum branch length.  Branches below this cutoff "
                             "are collapsed into polytomies (default 0.5).")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.boot:
        trees = Phylo.parse(args.tree, 'newick')
        ctree = consensus(trees, cutoff=args.cutoff)
    else:
        try:
            ctree = Phylo.read(args.tree, 'newick')
        except:
            print("Detected multiple trees in file, handling as bootstrap")
            trees = Phylo.parse(args.tree, 'newick')
            ctree = consensus(trees, cutoff=args.cutoff)

    # sequence labels keyed by integers mapping to tips
    label_dict = parse_labels(args.labels)
    tree = annotate_tree(ctree, label_dict, minlen=args.minlen)
    obj = serialize_tree(tree)
    args.outfile.write(json.dumps(obj, indent=2))
