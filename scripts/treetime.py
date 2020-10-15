import argparse
from subprocess import Popen, PIPE, check_call
import json
from tempfile import NamedTemporaryFile
import os
import math
from io import StringIO
from datetime import date
import re
import sys
import bisect

# third party libraries
from Bio import Phylo
from scipy.stats import poisson
from scipy.optimize import root

# local libraries
from seq_utils import *
from db_utils import dump_raw_by_lineage, retrieve_seqs
from minimap2 import minimap2, encode_diffs


def fromisoformat(dt):
    """ Support versions earlier than Python 3.7 """
    year, month, day = map(int, dt.split('-'))
    return date(year, month, day)


class QPois:
    """
    Cache the quantile transition points for Poisson distribution for a given
    rate <L> and varying time <t>, s.t. \exp(-Lt)\sum_{i=0}^{k} (Lt)^i/i! = Q.
    """
    def __init__(self, quantile, rate, maxtime, origin='2019-12-01'):
        self.q = quantile
        self.rate = rate
        self.maxtime = maxtime
        self.origin = fromisoformat(origin)

        self.timepoints = self.compute_timepoints()

    def objfunc(self, x, k):
        """ Use root-finding to find transition point for Poisson CDF """
        return self.q - poisson.cdf(k=k, mu=self.rate*x)

    def compute_timepoints(self, maxk=100):
        """ Store transition points until time exceeds maxtime """
        timepoints = []
        t = 0
        for k in range(maxk):
            res = root(self.objfunc, x0=t, args=(k, ))
            if not res.success:
                print("Error in QPois: failed to locate root, q={} k={} rate={}".format(
                    self.q, k, self.rate))
                print(res)
                break
            t = res.x[0]
            if t > self.maxtime:
                break
            timepoints.append(t)
        return timepoints

    def lookup(self, time):
        """ Retrieve quantile count, given time """
        return bisect.bisect(self.timepoints, time)

    def is_outlier(self, coldate, ndiffs):
        if type(coldate) is str:
            coldate = fromisoformat(coldate)
        dt = (coldate - self.origin).days
        qmax = self.lookup(dt)
        if ndiffs > qmax:
            return True
        return False


def filter_fasta(fasta_file, json_file, cutoff=10):
    """
    Filter the variants FASTA file for genomes representing clusters
    identified by hierarchical clustering (hclust.R).  Add variants
    that are observed N > [cutoff] times in the data.

    :param fasta_file:  path to FASTA file containing cluster sequences
    :param json_file:  path to JSON file with cluster information
    :return:  dict, filtered header-sequence pairs
    """
    result = {}
    fasta = dict([(h.split('|')[1], {'sequence': s, 'label': h}) for
                  h, s in iter_fasta(fasta_file)])
    clusters = json.load(json_file)
    for cluster in clusters:
        # record variant in cluster that is closest to root
        if type(cluster['nodes']) is list:
            # omit problematic cluster of one
            print(cluster['nodes'])
            continue

        # first entry is representative variant
        accn = list(cluster['nodes'].keys())[0]
        result.update({fasta[accn]['label']: fasta[accn]['sequence']})

        # extract other variants in cluster that have high counts
        major = [label for label, samples in
                 cluster['nodes'].items() if
                 len(samples) > cutoff and label != accn]
        for label in major:
            result.update({fasta[label]['label']: fasta[label]['sequence']})

    return result


def fasttree(fasta, binpath='fasttree2', seed=1):
    """
    Wrapper for FastTree2, passing FASTA as stdin and capturing the
    resulting Newick tree string as stdout.
    :param fasta: dict, header: sequence pairs
    :return: str, Newick tree string
    """
    in_str = ''
    for h, s in fasta.items():
        accn = h.split('|')[1]
        in_str += '>{}\n{}\n'.format(accn, s)
    p = Popen([binpath, '-nt', '-quote', '-seed', str(seed)],
              stdin=PIPE, stdout=PIPE)
    # TODO: exception handling with stderr?
    stdout, stderr = p.communicate(input=in_str.encode('utf-8'))
    return stdout.decode('utf-8')


def treetime(nwk, fasta, outdir, binpath='treetime', clock=None):
    """
    :param nwk: str, Newick tree string from fasttree()
    :param fasta: dict, header-sequence pairs
    :param outdir:  path to write output files
    :param clock: float, clock rate to constrain analysis - defaults
                  to None (no constraint)
    :return:  path to NEXUS output file
    """
    # extract dates from sequence headers
    datefile = NamedTemporaryFile('w', delete=False)
    datefile.write('name,date\n')
    alnfile = NamedTemporaryFile('w', delete=False)
    for h, s in fasta.items():
        # TreeTime seems to have trouble handling labels with spaces
        _, accn, coldate = h.split('|')
        datefile.write('{},{}\n'.format(accn, coldate))
        alnfile.write('>{}\n{}\n'.format(accn, s))
    datefile.close()
    alnfile.close()

    with NamedTemporaryFile('w', delete=False) as nwkfile:
        nwkfile.write(nwk.replace(' ', ''))

    call = [binpath, '--tree', nwkfile.name,
            '--aln', alnfile.name, '--dates', datefile.name,
            '--outdir', outdir]
    if clock:
        call.extend(['--clock-rate', str(clock)])
    check_call(call)

    nexus_file = os.path.join(outdir, 'timetree.nexus')
    if not os.path.exists(nexus_file):
        print("Error: missing expected NEXUS output file {}".format(nexus_file))
        return None
    return nexus_file


def date2float(isodate):
    """ Convert ISO date string to float """
    year, month, day = map(int, isodate.split('-'))
    dt = date(year, month, day)
    origin = date(dt.year, 1, 1)
    td = (dt-origin).days
    return dt.year + td/365.25


def parse_nexus(nexus_file, fasta, date_tol):
    """
    @param nexus_file:  str, path to write Newick tree string
    @param fasta:  dict, {header: seq} from filter_fasta()
    @param date_tol:  float, tolerance in tip date discordance
    """
    coldates = {}
    for h, _ in fasta.items():
        _, accn, coldate = h.split('|')
        coldates.update({accn: date2float(coldate)})

    # extract comment fields and store date estimates
    pat = re.compile('([^)(,:]+):([0-9]+\.[0-9]+)\[[^d]+date=([0-9]+\.[0-9]+)\]')

    # extract date estimates and internal node names
    remove = []
    with open(nexus_file) as handle:
        for line in handle:
            for m in pat.finditer(line):
                node_name, branch_length, date_est = m.groups()
                coldate = coldates.get(node_name, None)
                if coldate and abs(float(date_est) - coldate) > date_tol:
                    sys.stdout.write('removing {}:  {:0.3f} < {}\n'.format(
                        node_name, coldate, date_est
                    ))
                    sys.stdout.flush()
                    remove.append(node_name)

    # second pass to excise all comment fields
    pat = re.compile('\[&U\]|\[&mutations="[^"]*",date=[0-9]+\.[0-9]+\]')
    nexus = ''
    for line in open(nexus_file):
        nexus += pat.sub('', line)

    # read in tree to prune problematic tips
    phy = Phylo.read(StringIO(nexus), format='nexus')
    for node_name in remove:
        phy.prune(node_name)

    for node in phy.get_terminals():
        node.comment = None

    for node in phy.get_nonterminals():
        if node.name is None and node.confidence:
            node.name = node.confidence
            node.confidence = None
        node.comment = None

    Phylo.write(phy, file=nexus_file.replace('.nexus', '.nwk'),
                format='newick')


def filter_outliers(iter, origin='2019-12-01', rate=8e-4*29900/365., cutoff=0.005, maxtime=1e3):
    """
    Exclude genomes that contain an excessive number of genetic differences
    from the reference, assuming that the mean number of differences increases
    linearly over time and that the variation around this mean follows a
    Poisson distribution.
    :param iter:  generator, returned by encode_diffs()
    :param origin:  str, date of root sequence in ISO format (yyyy-mm-dd)
    :param rate:  float, molecular clock rate (subs/genome/day)
    :param cutoff:  float, use 1-cutoff to compute quantile of Poisson
                    distribution
    :param maxtime:  int, maximum number of days to cache Poisson quantiles
    :yield:  tuples from generator that pass filter
    """
    qp = QPois(quantile=1-cutoff, rate=rate, maxtime=maxtime, origin=origin)
    for qname, diffs, missing in iter:
        coldate = qname.split('|')[-1]
        if coldate.count('-') != 2:
            continue
        ndiffs = len(diffs)
        if qp.is_outlier(coldate, ndiffs):
            # reject genome with too many differences given date
            continue
        yield qname, diffs, missing


def retrieve_genomes(db="data/gsaid.db", ref_file='data/MT291829.fa', reflen=29774, misstol=300):
    """
    Query database for Pangolin lineages and then retrieve the earliest
    sampled genome sequence for each.  Export as FASTA for TreeTime analysis.
    :param db:  str, path to sqlite3 database
    :return:  list, (header, sequence) tuples
    """

    # load and parse reference genome
    with open(ref_file) as handle:
        _, refseq = convert_fasta(handle)[0]

    # allocate lists
    coldates = []
    lineages = []
    seqs = []

    for lineage, fasta in dump_raw_by_lineage(db):
        mm2 = minimap2(fasta=fasta, ref=ref_file)
        intermed = []

        iter = encode_diffs(mm2, reflen=reflen)
        for row in filter_outliers(iter):
            # exclude genomes too divergent from expectation
            if total_missing(row) > misstol:
                continue

            qname, _, _ = row
            _, coldate = parse_label(qname)
            intermed.append([coldate, row])

        if len(intermed) == 0:
            continue
        intermed.sort()  # defaults to increasing order
        coldate, row = intermed[0]  # earliest valid genome

        # update lists
        lineages.append(lineage)
        coldates.append(coldate)

        # reconstruct aligned sequence from feature vector
        seq = apply_features(row, refseq=refseq)
        seqs.append(seq)

    # generate new headers in {name}|{accession}|{date} format expected by treetime()
    headers = map(lambda xy: '|{}|{}'.format(*xy), zip(lineages, coldates))
    return dict(zip(headers, seqs))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate inputs for TreeTime analysis."
    )
    parser.add_argument('--db', type=str, default='data/gsaid.db',
                        help='input, sqlite3 database')
    parser.add_argument('--ref', type=str, default='data/MT291829.fa',
                        help='input, FASTA file with reference genome')
    parser.add_argument('--reflen', type=int, default=29774)
    parser.add_argument('--clock', type=float, default=8e-4,
                        help='optional, specify molecular clock rate for '
                             'constraining Treetime analysis (default 8e-4).')
    parser.add_argument('--datetol', type=float, default=0.1,
                        help='optional, exclude tips from time-scaled tree '
                             'with high discordance between estimated and '
                             'known sample collection dates (year units,'
                             'default: 0.1)')
    parser.add_argument('--outdir', default='data/',
                        help='optional, directory to write TreeTime output files')
    parser.add_argument('--ft2bin', default='fasttree2',
                        help='optional, path to fasttree2 binary executable')
    parser.add_argument('--ttbin', default='treetime',
                        help='optional, path to treetime binary executable')
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()

    fasta = retrieve_genomes(args.db, ref_file=args.ref, reflen=args.reflen)
    nwk = fasttree(fasta, binpath=args.ft2bin)
    nexus_file = treetime(nwk, fasta, outdir=args.outdir, binpath=args.ttbin,
                          clock=args.clock)
    parse_nexus(nexus_file, fasta, date_tol=args.datetol)
