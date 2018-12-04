from __future__ import print_function
import pysam
import operator
import random
import gzip
import os

HERE = os.path.dirname(__file__)
def xopen(path): return gzip.open(path) if path.endswith(".gz") else open(path)

class Sample(object):
    __slots__ = 'family_id id paternal_id maternal_id mom dad kids i'.split()
    def __init__(self, line):
        toks = line.rstrip().split("\t")
        self.family_id = toks[0]
        self.id = toks[1]
        self.paternal_id = toks[2]
        self.maternal_id = toks[3]
        self.kids = []
        self.i = -1 # index in the vcf.

    def __repr__(self):
        return "Sample(id:{id})".format(id=self.id)

def parse_ped(path, vcf_samples=None):
    if path is None:
        return {}
    samples = []
    look = {}
    for line in open(path):
        samples.append(Sample(line))
        look[samples[-1].id] = samples[-1]

    for s in samples:
        s.dad = look.get(s.paternal_id)
        if s.dad is not None:
            s.dad.kids.append(s)
        s.mom = look.get(s.maternal_id)
        if s.mom is not None:
            s.mom.kids.append(s)

    # match these samples to the ones in the VCF.
    if vcf_samples is not None:
        result = []
        for i, vs in enumerate(vcf_samples):
            if not vs in look: continue
            result.append(next(s for s in samples if s.id == vs))
            result[-1].i = i
        samples = result

    return {s.id: s for s in samples}


def get_names_to_bams(bams):
    """
    get mapping from names (read group samples) to bam paths)
    this is useful because the VCF has the names and we'll want the bam paths
    for those samples
    """
    names = {}
    for p in bams:
        b = pysam.AlignmentFile(p)
        names[b.header["RG"][0]['SM']] = p
    return names


cmp_lookup = {
        '>': operator.gt, # e.g. DHFC < 0.5
        '<': operator.lt,
        '<=': operator.le,
        '>=': operator.ge,
        '==': operator.eq,
        'contains': operator.contains, # e.g. CSQ contains HIGH
        }

def tryfloat(v):
    try:
        return float(v)
    except:
        return v

def to_exprs(astr):
    """
    an expr is just a 3-tuple of "name", fn, value"
    e.g. "DHFFC", operator.lt, 0.7"
    >>> to_exprs("DHFFC < 0.5 & SVTYPE == 'DEL'")
    [('DHFFC', <built-in function lt>, 0.5), ('SVTYPE', <built-in function eq>, 'DEL')]

    >>> to_exprs("CSQ contains 'HIGH'")
    [('CSQ', <built-in function contains>, 'HIGH')]
    """
    astr = (x.strip() for x in astr.strip().split("&"))
    result = []
    for a in astr:
        a = [x.strip() for x in a.split()]
        assert len(a) == 3, ("bad expression", a)
        assert a[1] in cmp_lookup, ("comparison:" + a[1] + " not supported. must be one of:" + ",".join(cmp_lookup.keys()))
        result.append((a[0], cmp_lookup[a[1]], tryfloat(a[2].strip("'").strip('"'))))
    return result

def check_expr(vdict, expr):
    """
    >>> check_expr({"CSQ": "asdfHIGHasdf"}, to_exprs("CSQ contains 'HIGH'"))
    True

    >>> check_expr({"CSQ": "asdfHIGHasdf", "DHFC": 1.1}, to_exprs("CSQ contains 'HIGH' & DHFC < 0.5"))
    False

    >>> check_expr({"CSQ": "asdfHIGHasdf", "DHFC": 1.1}, to_exprs("CSQ contains 'HIGH' & DHFC < 1.5"))
    True
    """

    # a single set of exprs must be "anded"
    for name, fcmp, val in expr:
        # NOTE: asking for a missing annotation will return false.
        if not name in vdict: return False
        if not fcmp(vdict[name], val):
            return False
    return True

def make_single(vdict):
    """
    >>> d = {"xx": (1,)}
    >>> make_single(d)
    >>> d
    {'xx': 1}
    """
    for k in vdict.keys():
        if isinstance(vdict[k], tuple) and len(vdict[k]) == 1:
            vdict[k] = vdict[k][0]

def main(args, pass_through_args):
    vcf = pysam.VariantFile(args.vcf)
    vcf_samples = vcf.header.samples
    vcf_samples_set = set(vcf_samples)

    filters = [to_exprs(f) for f in args.filter]

    ped_samples = parse_ped(args.ped, vcf_samples)

    if not os.path.exists(args.out_dir):
      os.makedirs(args.out_dir)

    names_to_bams = get_names_to_bams(args.bams)

    for v in vcf:
        svtype = v.info.get("SVTYPE", "SV")
        if svtype == "BND": continue

        gts = [s.get("GT", (None, None)) for s in v.samples.values()]

        if sum(None in g for g in gts) >= args.min_call_rate * len(vcf_samples): continue
        # requisite hets
        if sum(sum(x) == 1 for x in gts if not None in x) > args.max_hets: continue
        if not any(sum(x) > 0 for x in gts if not None in x): continue

        test_idxs = [i for i, gt in enumerate(gts) if not None in gt and sum(gt) > 0]
        test_samples = [s for i, s in enumerate(v.samples.values()) if i in test_idxs]

        odict = make_single(dict(v.info.items()))

        idxs = []
        for i, ts in enumerate(test_samples):
            vdict = odict.copy()
            vdict.update(make_single(dict(ts.items())))

            if any(check_expr(vdict, fs) for fs in filters):
                idxs.append(i)

        vsamples = [vcf_samples[i] for i in idxs]
        bams = [names_to_bams[s] for s in vsamples]
        # try to get family members
        if args.ped is not None:
            for vs in vsamples:
                s = ped_samples.get(vs)
                if s is None: continue
                if s.mom is not None and not s.mom.id in vsamples and s.mom.id in vcf_samples_set:
                    vsamples.append(s.mom.id)
                    bams.append(names_to_bams[s.mom.id])
                if s.dad is not None and not s.dad.id in vsamples and s.dad.id in vcf_samples_set:
                    vsamples.append(s.dad.id)
                    bams.append(names_to_bams[s.dad.id])
                for kid in s.kids:
                    if not kid.id in vsamples and kid.id in vcf_samples_set:
                        vsamples.append(kid.id)
                        bams.append(names_to_bams[kid.id])

            if len(bams) > 1.5 * args.max_hets: break

        elif len(bams) < 6:
            # extend with some controls:
            hom_ref_idxs = [i for i, gt in enumerate(gts) if len(gt) == 2 and gt[0] == 0 and gt[1] == 0]
            if len(hom_ref_idxs) > 3:
              random.shuffle(hom_ref_idxs)
              hom_ref_idxs = hom_ref_idxs[:3]

            hsamples = [vcf_samples[i] for i in hom_ref_idxs]
            bams.extend(names_to_bams[s] for s in hsamples)
            vsamples += ["control-sample: " + s for s in hsamples]

        fig_path = "{out_dir}/{svtype}_{chrom}_{start}_{end}.{itype}".format(
                svtype=svtype,
                out_dir=args.out_dir, chrom=v.chrom, start=v.start, end=v.stop, itype=args.output_type)

        print("python {here}/samplot.py {extra_args} --minq 0 -n {titles} {svtype} -c {chrom} -s {start} -e {end} -o {fig_path} -d 1 -w 2000 -b {bams}".format(here=HERE,
            extra_args=" ".join(pass_through_args), bams=" ".join(bams),
            titles=" ".join(vsamples),
            svtype="-t " + svtype if svtype != "SV" else "",
            fig_path=fig_path,
            chrom=v.chrom, start=v.start, end=v.stop))


if __name__ == "__main__":
    import argparse
    import sys
    import doctest
    if sys.argv[1] == "test":
        r = doctest.testmod()
        print(r)
        sys.exit(r.failed)

    p = argparse.ArgumentParser("note that additional arguments are passed through to samplot.py")
    p.add_argument("--vcf", "-v", help="VCF file containing structural variants")
    p.add_argument("-d", "--out-dir", help="path to write output PNGs", default="samplot-out")
    p.add_argument("--ped", help="path ped (or .fam) file")
    p.add_argument("--min-call-rate", type=float, help="only plot variants with at least this call-rate", default=0.9)
    p.add_argument("--filter", action="append", help="simple filter that samples" +
            " must meet. Join multiple filters with '&' and specify --filter multiple times for 'or'" +
            " e.g. DHFFC < 0.7 & SVTYPE = 'DEL'" , default=[])
    p.add_argument("-O", "--output-type", choices=("png", "pdf", "eps", "jpg"), help="type of output figure", default="png")
    p.add_argument("--max-hets", type=int, help="only plot variants with at most this many heterozygotes", default=10)
    p.add_argument("-b", "--bams", type=str, nargs="+", help="Space-delimited list of BAM/CRAM file names", required=True)

    a, pass_through_args = p.parse_known_args()
    main(a, pass_through_args)
