#!/usr/bin/env python3
"""
Script to simulate TE excisions from a genome FASTA based on a BED file annotation.
It randomly removes features (TEs only) and adjusts the BED and genome sequence accordingly.
See the help text below for details.

New functionality: If a TE chosen for excision is one of:
   NEST_TE_IN_TE, NEST_TE_IN_GENE, or NON_NEST_GROUP_TE,
and its feature_ID contains "LTRlen" (and does NOT contain "_SOLO"),
then with a probability given by --soloLTR_freq (in percent) the excision is done partially.
In a partial excision the sequence from (START + LTRlen) to END is removed, and the BED
entry is updated so that its end becomes (START+LTRlen) and "_SOLO" is appended to the feature_ID,
while preserving supplemental info fields.

Additional filtering is applied to ensure that non-INTACT_LTR candidates are excluded:
  1) If the first supplemental attribute in NAME contains "CUT_BY", the candidate is not considered.
  2) For candidates with supplemental info fields, if any other entry within 100 lines (based on BED order)
     has an identical TSD, identical strand, and a NAME that is an exact or prefix match,
     the candidate is filtered out.
     
Note: Consolidation of nest groups is reserved for full-length excisions.
  
NEW EDIT:
For the non-fixed rate model (specified with "--rate"), the number of TE excisions is still determined
by: rate * generations * (sum of disrupted gene weights). However, when selecting excision events,
the sampling weight is now modified so that gene-disrupting TEs (those classified as NEST_TE_IN_GENE)
are further favored. Their base weight (based on feature length and parameter k) is multiplied by:
    (1 + sel_coeff * (gene_weight) * generations)
where gene_weight is obtained from gene_selection.tsv (or defaults to 1), and sel_coeff is provided by --sel_coeff.
This adjustment reflects selection acting against individuals that fail to delete gene-disrupting TEs.
  
Additionally, if a fixed excision rate is provided with --fix_ex, the gene weights and selection coefficient are bypassed.
  
The script also produces publication-quality figures for the log-normal distribution and the weighted candidate selection curve.

NEW CHROMATIN BIAS:
In rate mode, the likelihood of a TE excision is further modulated by its chromatin context.
A new parameter --euch_het_buffer specifies a buffer (in bp) around gene features to be treated as euchromatin.
Any TE whose midpoint falls in one of these regions will have its base weight multiplied
by --euch_het_bias (e.g. 1.1 increases the weight by 10%). Regions outside these merged gene intervals
are assumed to be heterochromatin and receive no additional boost.
"""
import argparse
import sys
import random
import copy
import os
import math
from collections import defaultdict
from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
import re
import numpy as np
import matplotlib.pyplot as plt
import concurrent.futures

# =============================================================================
# Data Structures and Helpers
# =============================================================================

class BedEntry:
    def __init__(self, chrom, start, end, name, strand, tsd, lineno, tag=None):

        self.chrom = chrom
        self.start = int(start)
        self.end = int(end)
        self.name = name
        self.strand = strand
        self.tsd = tsd
        self.lineno = lineno
        self.tag = tag
        self.group = None
        self.subtype = None
        self.feature_id = name.split(';')[0]
        self.supp = name.split(';')[1:] if ';' in name else []

#   def __str__(self):
#       return "\t".join([self.chrom, str(self.start), str(self.end), self.name, self.strand, self.tsd])
    def __str__(self):
        fields = [self.chrom,
                  str(self.start),
                  str(self.end),
                  self.name,
                  self.strand,
                  self.tsd]
        if self.tag is not None:
            fields.append(self.tag)
        return "\t".join(fields)

    def length(self):
        return self.end - self.start

def parse_bed(bed_file):
    entries = []
    with open(bed_file) as f:
        for lineno, line in enumerate(f):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 6:
                sys.exit(f"Error: Line {lineno+1} in BED file does not have at least 6 columns.")
            # grab optional 7th column as tag
            tag = fields[6] if len(fields) > 6 else None
            entries.append(
                BedEntry(fields[0], fields[1], fields[2], fields[3], fields[4], fields[5], lineno, tag=tag)
            )
    return entries

def parse_fasta(fasta_file):
    recs = {}
    for rec in SeqIO.parse(fasta_file, "fasta"):
        recs[rec.id] = rec
    return recs

def build_euchromatin_intervals(entries, buffer):
    intervals_by_chrom = defaultdict(list)
    for e in entries:
        if e.feature_id.startswith("gene"):
            start = max(0, e.start - buffer)
            end = e.end + buffer
            intervals_by_chrom[e.chrom].append((start, end))
    merged = {}
    for chrom, ivals in intervals_by_chrom.items():
        ivals.sort(key=lambda x: x[0])
        merged_list = []
        cs, ce = ivals[0]
        for s, e in ivals[1:]:
            if s <= ce:
                ce = max(ce, e)
            else:
                merged_list.append((cs, ce))
                cs, ce = s, e
        merged_list.append((cs, ce))
        merged[chrom] = merged_list
    return merged

def classify_entries(entries):
    """
    Classify entries into groups.
      - Genes are those with feature_ID starting with 'gene'
      - A gene is 'disrupted' if its NAME contains a semicolon.
    Then identify nest groups.
    
    Nest groups occur when a BED entry (the middle one) has a supplemental attribute
    starting with 'NESTED_IN:' AND is immediately flanked (previous and next entries)
    by lines that share identical feature_ID (only the first part, ignoring supplemental info),
    strand and TSD.
    
    If the flanking feature_ID begins with 'gene', the nest group is a NEST_GROUP_GENE;
    otherwise it is a NEST_GROUP_TE.
    
    For nest groups, assign the following subtypes:
      - For TE nest groups:
           CUT_PAIR_TE_1, NEST_TE_IN_TE, CUT_PAIR_TE_2.
      - For gene nest groups:
           CUT_PAIR_GENE_1, NEST_TE_IN_GENE, CUT_PAIR_GENE_2.
    All other entries with feature_ID starting with 'gene' become NON_NEST_GROUP_GENES.
    All remaining entries become NON_NEST_GROUP_TE.
    """
    n = len(entries)
    i = 0
    used = [False]*n
    nest_groups = {}
    group_id = 0
    
    while i < n:
        if i > 0 and i < n-1 and not used[i-1] and not used[i] and not used[i+1]:
            middle = entries[i]
            if middle.supp and middle.supp[0].startswith("NESTED_IN:"):
                prev = entries[i-1]
                nxt = entries[i+1]
                # Compare based on feature_ID (first attribute) only.
                if (prev.feature_id == nxt.feature_id and
                    prev.strand == nxt.strand and
                    prev.tsd == nxt.tsd):
                    group_id += 1
                    if prev.feature_id.startswith("gene"):
                        prev.subtype = "CUT_PAIR_GENE_1"
                        middle.subtype = "NEST_TE_IN_GENE"
                        nxt.subtype = "CUT_PAIR_GENE_2"
                    else:
                        prev.subtype = "CUT_PAIR_TE_1"
                        middle.subtype = "NEST_TE_IN_TE"
                        nxt.subtype = "CUT_PAIR_TE_2"
                    for ent in (prev, middle, nxt):
                        ent.group = group_id
                        used[ent.lineno] = True
                    nest_groups[group_id] = (prev, middle, nxt)
                    i += 2
                    continue
        i += 1

    for ent in entries:
        if ent.subtype is None:
            if ent.feature_id.startswith("gene"):
                ent.subtype = "NON_NEST_GROUP_GENES"
            else:
                ent.subtype = "NON_NEST_GROUP_TE"
    return entries, nest_groups

# =============================================================================
# Calculate number of TE excisions to make
# =============================================================================

def calculate_excision_count(entries, rate, generations, gene_weights):
    """
    For disrupted genes, use the disrupted_gene_weight from gene_weights instead of counting each as 1.
    A gene is considered disrupted if its feature_ID starts with 'gene' and its NAME contains a semicolon.
    Sum the weights of all unique disrupted genes and calculate:
         lambda_val = rate * generations * (sum of disrupted gene weights)
    Then sample the number of TE excisions from a Poisson distribution with lambda_val.
    """
    disrupted_genes = {e.feature_id for e in entries if e.feature_id.startswith("gene") and (';' in e.name)}
    weight_sum = sum(gene_weights.get(gene, 1) for gene in disrupted_genes)
    print(f"Disrupted genes (unique): {len(disrupted_genes)}")
    print(f"Sum of disrupted gene weights: {weight_sum:.4f}")
    
    lambda_val = rate * generations * weight_sum
    excision_count = np.random.poisson(lambda_val)
    print(f"Lambda for Poisson (rate mode): {lambda_val:.4f}")
    print(f"Number of TE excisions to simulate: {excision_count}")
    return excision_count

# =============================================================================
# Select Removal Events (Weighted by length, chromatin state and selection for gene-disrupting TEs)
# =============================================================================

def select_removals(entries, nest_groups, num_excision, seed, k, gene_weights, generations, sel_coeff,
                    euch_intervals=None, euch_het_bias=1.0):
    """
    Eligible for removal are:
       - NON_NEST_GROUP_TE entries,
       - In nest groups of type TE: the middle entry (NEST_TE_IN_TE) is eligible,
         and flanking entries (CUT_PAIR_TE_1 and CUT_PAIR_TE_2) are eligible.
       - In nest groups of type GENE: only the middle (NEST_TE_IN_GENE) is eligible.
       
    Genes (NON_NEST_GROUP_GENES, CUT_PAIR_GENE_1, CUT_PAIR_GENE_2) are immune.
    
    This version selects from eligible features using a weighted probability distribution based on length.
    For each eligible entry, let L = end - start, and Lmax be the maximum L among eligible entries.
    Then assign the base excision weight as:
         weight_base = exp( - k * (1 - (L / Lmax)) )
    Additionally, if euch_intervals is provided (rate mode), and the candidate’s midpoint falls within any
    euchromatin interval, multiply the weight by euch_het_bias.
    
    NEW EDIT: For candidates that are gene-disrupting (subtype NEST_TE_IN_GENE), their weight is multiplied
    by an additional factor: (1 + sel_coeff * gene_weight * generations), where gene_weight is obtained from gene_weights
    using the gene ID from the flanking entry in the nest group.
    
    Removal events are then sampled without replacement using these weights.
    
    For nest groups, if a candidate is chosen from a group, then other members from that group are skipped.
    """
    random.seed(seed)
    # Build eligible candidate list
    eligible = []
    for e in entries:
        if e.subtype in ["NON_NEST_GROUP_TE", "CUT_PAIR_TE_1", "CUT_PAIR_TE_2",
                         "NEST_TE_IN_TE", "NEST_TE_IN_GENE"]:
            eligible.append(e)
    
    if not eligible:
        return set()
    
    # Determine L95 (95th percentile) among eligible entries to mitigate long‐outliers
    lengths = [e.length() for e in eligible]
    L95 = float(np.percentile(lengths, 95))

    # Pre-calculate weights for each eligible candidate, clamping lengths > L95 to L95
    weights = {}
    for e in eligible:
        L_eff = min(e.length(), L95)
        base_weight = math.exp(-k * (1 - (L_eff / L95)))
        # Apply chromatin state bias if euch_intervals is provided.
        if euch_intervals is not None:
            # Use the midpoint of the TE candidate.
            mid = (e.start + e.end) // 2
            if e.chrom in euch_intervals:
                for (istart, iend) in euch_intervals[e.chrom]:
                    if istart <= mid <= iend:
                        base_weight *= euch_het_bias
                        break
        # If the candidate is gene-disrupting, further weight it.
        if e.subtype == "NEST_TE_IN_GENE" and e.group is not None:
            # For nest groups with gene disruption, use the gene ID from one of the flanking entries.
            group = nest_groups.get(e.group)
            if group:
                # Assume the first flanking entry (CUT_PAIR_GENE_1) has the gene ID.
                gene_id = group[0].feature_id
                gene_weight = gene_weights.get(gene_id, 1)
                multiplier = 1 + sel_coeff * gene_weight * generations
                base_weight *= multiplier
        weights[id(e)] = base_weight

    removals = set()
    group_removed = {}
    
    # Perform weighted sampling without replacement.
    # Continue until we have num_excision events or no eligible candidates remain.
    while len(removals) < num_excision and eligible:
        total_weight = sum(weights[id(e)] for e in eligible)
        # Pick a random threshold in [0, total_weight)
        r = random.uniform(0, total_weight)
        cumulative = 0.0
        chosen = None
        for e in eligible:
            cumulative += weights[id(e)]
            if cumulative >= r:
                chosen = e
                break
        if chosen is None:
            chosen = eligible[-1]
                        
        if chosen.group is not None:
            # detect TE‐only nest groups by inspecting the flanking subtype
            grp = nest_groups.get(chosen.group)
            is_te_group = grp and grp[0].subtype.startswith("CUT_PAIR_TE")

            if is_te_group:
                # TE‐nest groups remain fully eligible: just remove the chosen entry
                eligible = [e for e in eligible if e is not chosen]
            else:
                # gene‐nest groups: keep the old behavior
                if chosen.group in group_removed:
                    eligible = [e for e in eligible if e is not chosen]
                    continue
                group_removed[chosen.group] = ("middle"
                                               if chosen.subtype == "NEST_TE_IN_GENE"
                                               else "flank")
                eligible = [e for e in eligible if e.group != chosen.group]
        else:
            eligible = [e for e in eligible if e is not chosen]
        
        removals.add(chosen)
    print(f"Selected {len(removals)} removal events.")
    return removals

# =============================================================================
# Simulation: Remove sequences and adjust bed coordinates
# =============================================================================

def simulate_excision(genome_records, entries, nest_groups, removals, soloLTR_freq):
    """
    This function modifies the genome sequences and bed entries.
    
    There are two main cases:
    
    1) Simple removal (NON_NEST_GROUP_TE, or flanking entries in a nest group that get excised)
       - For a given bed entry to remove, remove from the genome the sequence from start to (end - TSD_length)
         (if TSD is not 'NA'; if TSD=='NA', remove the whole region).
       - Remove the bed entry.
       - For subsequent coordinates on that chromosome, shift by the removed length.
       
    2) Nested removal: if a nest-group middle (NEST_TE_IN_TE or NEST_TE_IN_GENE) is selected
       and it is NOT a partial (solo) excision, then:
       - Remove the sequence corresponding to the middle entry.
       - Then consolidate the flanking entries into one bed entry.
         The new bed entry will have:
             start = CUT_PAIR_x_1.start (unchanged)
             end = CUT_PAIR_x_2.end - (middle.length())
         (i.e. the gap from removal is subtracted)
       - The consolidated entry replaces the three original ones.
       
    NEW: For a removal event that qualifies as an INTACT_LTR candidate (its feature_ID contains 'LTRlen'
         and does not contain '_SOLO') and its subtype is one of [NEST_TE_IN_TE, NEST_TE_IN_GENE, NON_NEST_GROUP_TE],
         we further filter candidates:
           a) If the first supplemental attribute in the NAME contains "CUT_BY", it is not considered.
           b) If the candidate has supplemental info fields and any other entry within 100 lines (before or after)
              has identical TSD and strand and a NAME that is an exact or prefix match, it is not considered.
         For those candidates passing these filters, with probability soloLTR_freq (in percent) a partial excision is performed.
         In a partial excision, the sequence from (start + LTRlen) to end is removed, and the BED entry is updated so that
         end becomes (start+LTRlen) and '_SOLO' is appended to the feature_ID while preserving supplemental info.
         Note: No consolidation is performed after a partial excision.
    """
    def qualifies_as_intact_ltr(e, all_entries):
        # Condition (a): if the first supplemental attribute contains "CUT_BY", reject.
        if e.supp and "CUT_BY" in e.supp[0]:
            return False
        # Condition (b): if there is at least one supplemental field, check nearby entries.
        if e.supp:
            for other in all_entries:
                if other is e:
                    continue
                if abs(other.lineno - e.lineno) <= 100:
                    if other.tsd == e.tsd and other.strand == e.strand:
                        # Check if the NAMEs have an exact or prefix match.
                        if e.name.startswith(other.name) or other.name.startswith(e.name):
                            return False
        return True

    # Iterate over removals in a deterministic order.
    sorted_removals = sorted(removals, key=lambda x: (x.chrom, x.start, x.end, x.lineno))
    
    # Determine which removal events will be partial excisions.
    # Map entry id -> LTR offset (extracted from "LTRlen:XXX") for those chosen for partial excision.
    partial_info = {}
    for e in sorted_removals:
        if e.subtype in ["NEST_TE_IN_TE", "NON_NEST_GROUP_TE"]:
#           if ("LTRlen" in e.feature_id) and ("_SOLO" not in e.feature_id):
            if ("LTRlen" in e.feature_id) and ("_SOLO" not in e.feature_id) and ("_FRAG" not in e.feature_id):
                if not qualifies_as_intact_ltr(e, entries):
                    continue
                m = re.search(r"LTRlen:(\d+)", e.feature_id)
                if m:
                    ltr_val = int(m.group(1))
                    if random.random() < (soloLTR_freq / 100.0):
                        partial_info[id(e)] = ltr_val

    removals_by_chrom = defaultdict(list)
    new_entries = []
    to_remove = set(sorted_removals)
    nest_middle_removed = set()
    for e in sorted_removals:
        if e.subtype in ["NEST_TE_IN_TE", "NEST_TE_IN_GENE"]:
            if id(e) not in partial_info:
                nest_middle_removed.add(e.group)
    
    # Process nest groups.
    for gid, group in nest_groups.items():
        is_te_group = group[0].subtype.startswith("CUT_PAIR_TE")
        can_consolidate = (
            gid in nest_middle_removed
            and (not is_te_group or (group[0] not in to_remove and group[2] not in to_remove))
            # **tag test**: only if both flanks agree (both have tag, or both None)
            and (group[0].tag == group[2].tag)
        )
        if can_consolidate:
            rem_len = group[1].length()
            chrom = group[1].chrom
            removals_by_chrom[chrom].append((group[1].start, group[1].end, rem_len))
            print(f"Excision (nest group consolidation): Group {gid} …")
            new_start = group[0].start
            new_end = group[2].end - rem_len
            new_name = group[0].feature_id
            new_strand = group[0].strand
            new_tsd = group[0].tsd
            # carry over the common tag
            new_tag = group[0].tag
            new_entry = BedEntry(chrom, new_start, new_end, new_name,
                                 new_strand, new_tsd, -1, tag=new_tag)
            new_entry.subtype = "CONSOLIDATED_NEST"
            new_entries.append(new_entry)
        else:
            # either not eligible or tags disagree → process each member individually
            for e in group:
                if e in to_remove:
                    if id(e) in partial_info:
                        ltr_val = partial_info[id(e)]
                        rem_len = e.end - (e.start + ltr_val)
                        removals_by_chrom[e.chrom].append( (e.start + ltr_val, e.end, rem_len) )
                        print(f"Partial excision in group {gid}: {e.chrom}:{e.start}-{e.end} reduced to {e.start}-{e.start+ltr_val} (removed {rem_len} bases)")
                        e.end = e.start + ltr_val
                        new_feature_id = e.feature_id + "_SOLO"
                        e.feature_id = new_feature_id
                        if e.supp:
                            e.name = new_feature_id + ";" + ";".join(e.supp)
                        else:
                            e.name = new_feature_id
                        new_entries.append(e)
                    else:
                        tsd_len = len(e.tsd) if e.tsd != "NA" else 0
                        rem_len = e.length() - tsd_len
                        removals_by_chrom[e.chrom].append( (e.start, e.end - tsd_len, rem_len) )
                        print(f"Full excision in nest group {gid}: {e.chrom}:{e.start}-{e.end} (removed {rem_len} bases)")
                else:
                    new_entries.append(e)
                    
    # Process non-group entries.
    for e in entries:
        if e.group is None:
            if e in to_remove:
                if id(e) in partial_info:
                    ltr_val = partial_info[id(e)]
                    rem_len = e.end - (e.start + ltr_val)
                    removals_by_chrom[e.chrom].append( (e.start + ltr_val, e.end, rem_len) )
                    print(f"Partial excision: {e.chrom}:{e.start}-{e.end} reduced to {e.chrom}:{e.start}-{e.start+ltr_val} (removed {rem_len} bases)")
                    e.end = e.start + ltr_val
                    new_feature_id = e.feature_id + "_SOLO"
                    e.feature_id = new_feature_id
                    if e.supp:
                        e.name = new_feature_id + ";" + ";".join(e.supp)
                    else:
                        e.name = new_feature_id
                    new_entries.append(e)
                else:
                    tsd_len = len(e.tsd) if e.tsd != "NA" else 0
                    rem_len = e.length() - tsd_len
                    removals_by_chrom[e.chrom].append( (e.start, e.end - tsd_len, rem_len) )
                    print(f"Full excision: {e.chrom}:{e.start}-{e.end} (removed {rem_len} bases)")
            else:
                new_entries.append(e)
    
    updated_genome = {}
    for chrom, rec in genome_records.items():
        seq = list(str(rec.seq))
        events = sorted(removals_by_chrom.get(chrom, []), key=lambda x: x[0])
        total_shift = 0
        for (rstart, rend, rlen) in events:
            adj_start = rstart - total_shift
            adj_end = rend - total_shift
            del seq[adj_start:adj_end]
            total_shift += rlen
        updated_seq = "".join(seq)
        new_rec = SeqRecord(Seq(updated_seq), id=rec.id, description="")
        updated_genome[chrom] = new_rec

    for entry in new_entries:
        shift = 0
        events = sorted(removals_by_chrom.get(entry.chrom, []), key=lambda x: x[0])
        for (rstart, rend, rlen) in events:
            if rstart < entry.start:
                shift += rlen
        entry.start -= shift
        entry.end -= shift

    new_entries.sort(key=lambda x: (x.chrom, x.start))
    return updated_genome, new_entries

# =============================================================================
# Failsafe consolidation for adjacent gene entries
# =============================================================================

def fail_safe_consolidation(bed_entries):
    """
    After simulation, if two or more gene entries with the same feature_ID, strand, and TSD
    are directly adjacent in the sorted BED, consolidate them into a single entry.
    In the consolidated entry, only the feature_ID is kept as NAME (i.e. supplemental attributes are removed).
    """
    consolidated = []
    # Group entries by chromosome.
    entries_by_chrom = defaultdict(list)
    for e in bed_entries:
        entries_by_chrom[e.chrom].append(e)
    
    for chrom, entries in entries_by_chrom.items():
        entries.sort(key=lambda x: x.start)
        i = 0
        while i < len(entries):
            curr = entries[i]
            # Only consolidate genes (feature_ID starting with "gene")
            if curr.feature_id.startswith("gene"):
                j = i + 1
                new_start = curr.start
                new_end = curr.end
                # Consolidate consecutive entries with same feature_ID, strand, and TSD.
                while (j < len(entries) and 
                       entries[j].feature_id.startswith("gene") and
                       entries[j].feature_id == curr.feature_id and
                       entries[j].strand == curr.strand and
                       entries[j].tsd == curr.tsd):
                    new_end = entries[j].end
                    j += 1
                if j > i + 1:
                    # Create a new consolidated entry.
                    new_entry = BedEntry(chrom, new_start, new_end, curr.feature_id, curr.strand, curr.tsd, curr.lineno)
                    new_entry.subtype = "CONSOLIDATED_FAILSAFE"
                    consolidated.append(new_entry)
                    i = j
                    continue
            consolidated.append(curr)
            i += 1
    # Sort the consolidated list before returning.
    consolidated.sort(key=lambda x: (x.chrom, x.start))
    return consolidated

# =============================================================================
# Write outputs
# =============================================================================

def write_fasta(genome_records, output_prefix):
    out_file = output_prefix + ".fasta"
    SeqIO.write(list(genome_records.values()), out_file, "fasta")
    print(f"Updated FASTA written to {out_file}")

def write_bed(bed_entries, output_prefix):
    out_file = output_prefix + ".bed"
    with open(out_file, "w") as f:
        for e in bed_entries:
            f.write(str(e) + "\n")
    print(f"Updated BED written to {out_file}")

# =============================================================================
# Plotting Functions
# =============================================================================

def plot_lognormal(sigma, outname):
    """
    Plot the log-normal gene selection curve.
    The lognormal distribution used has parameters: mu = sigma^2 and sigma.
    """
    mu = sigma ** 2
    # Define x range: avoid 0 and go up to a high quantile
    x_max = math.exp(mu + 3*sigma)
    x = np.linspace(0.001, x_max, 500)
    pdf = (1/(x * sigma * np.sqrt(2*math.pi)) *
           np.exp(- (np.log(x) - mu)**2 / (2*sigma**2)))
    plt.figure(figsize=(6,4))
    plt.plot(x, pdf, lw=2)
    plt.xlabel("Gene Weight")
    plt.ylabel("Probability Density")
    plt.title("Log-normal Distribution (μ = {:.2f}, σ = {:.2f})".format(mu, sigma))
    plt.tight_layout()
    plt.savefig(outname)
    plt.close()
    print(f"Log-normal distribution figure saved as {outname}")

def plot_weighted_candidate_curve(k, L95, outname):
    """
    Plot weighted candidate selection curve using the 95th‐percentile length (L95).
    Weight = exp( - k * (1 - (min(L, L95) / L95)) ) for L in [0, L95],
    and any L > L95 is treated as L95.
    """
    L = np.linspace(0, L95, 500)
    weight = np.exp(-k * (1 - (L / L95)))
    plt.figure(figsize=(6,4))
    plt.plot(L, weight, lw=2)
    plt.xlabel("Feature Length (L)")
    plt.ylabel("Excision Weight")
    plt.title("Weighted Candidate Selection Curve (k = {:.2f})".format(k))
    plt.tight_layout()
    plt.savefig(outname)
    plt.close()
    print(f"Weighted candidate selection curve figure saved as {outname}")

# =============================================================================
# Per-chromosome worker
# =============================================================================

def process_chrom(args_tuple):
    (chrom, entries, genome_record, nest_groups, excision_count,
     seed_i, k, gene_weights, generations, sel_coeff,
     euch_intervals, euch_het_bias, soloLTR_freq) = args_tuple

    # Ensure reproducibility per-chrom:
    random.seed(seed_i)
    np.random.seed(seed_i)

    # Select removal events for this chromosome
    removals = select_removals(
        entries, nest_groups, excision_count, seed_i, k,
        gene_weights, generations, sel_coeff,
        euch_intervals=euch_intervals, euch_het_bias=euch_het_bias
    )

    # Reseed before simulation to keep partial excision deterministic
    random.seed(seed_i + 1)

    # Simulate and adjust sequences and coords for this chrom
    updated_genome_chrom, updated_entries = simulate_excision(
        {chrom: genome_record},
        entries,
        nest_groups,
        removals,
        soloLTR_freq
    )

    return updated_genome_chrom, updated_entries

# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Simulate TE excisions from a genome based on a BED file.\n"
                    "Randomly remove TE features (with special handling of nest groups) and "
                    "update the genome FASTA and BED coordinates accordingly.\n\n"
                    "New: For INTACT_LTR elements (TE entries with 'LTRlen' in feature_ID and not '_SOLO'), "
                    "partial excision may be performed. Use --soloLTR_freq to set the percentage (e.g., 10 for 10%).\n"
                    "Additional filtering excludes candidates if the first supplemental attribute contains 'CUT_BY' "
                    "or if a nearby entry (within 100 lines) has an identical TSD, strand, and a NAME that is an exact or prefix match.\n\n"
                    "For disrupted genes, a weight is applied. By default, the number of TE excisions is calculated as:\n"
                    "    rate * generations * (sum of disrupted gene weights).\n"
                    "NEW: Use --fix_ex to bypass this calculation and specify a fixed excision rate (e.g., 1e-6).\n"
                    "      In this case, the number of excisions is calculated as: --fix_ex * genome_size * generations.\n\n"
                    "EDIT: For the non-fixed rate model (--rate), candidate excisions are now further weighted so that\n"
                    "gene-disrupting TEs (NEST_TE_IN_GENE) are favored. Their base weight (from feature length and --k)\n"
                    "is multiplied by (1 + sel_coeff * gene_weight * generations), where gene_weight comes from gene_selection.tsv.\n\n"
                    "CHROMATIN BIAS (rate mode only): Define euchromatin regions as any region within a buffer around a gene.\n"
                    "Candidates whose midpoints lie within these merged intervals get their weight multiplied by --euch_het_bias.\n"
                    "For example, '--euch_het_buffer 10000 --euch_het_bias 1.1' treats 10kb upstream and downstream of each gene\n"
                    "as euchromatin and gives candidate TE excisions in these regions a 10% weight boost.\n"
                    "Also, publication-quality figures are generated for the log-normal distribution and the weighted candidate selection curve.\n"
                    "Use --no_fig to disable figure generation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--genome", required=True, help="Input genome FASTA file.")
    parser.add_argument("--bed", required=True, help="Existing BED file with TE/gene coordinates.")
    parser.add_argument("--output", required=True, help="Output prefix (for .bed and .fasta).")
    parser.add_argument("--rate", type=float, default=1e-4, help="Rate of TE deletion per generation per disrupted gene weight.")
    parser.add_argument("--generations", type=int, default=1, help="Number of generations to simulate.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility.")
    parser.add_argument("--soloLTR_freq", type=float, default=0, help="Percent of INTACT_LTR excisions to perform partially.")
    parser.add_argument("--sigma", type=float, default=1.0, help="Sigma for log-normal distribution of gene weights.")
    parser.add_argument("--k", type=float, default=1.0, help="Decay rate for weighted excision selection.")
    parser.add_argument("--no_fig", action="store_true", help="Disable generating PDF figures.")
    parser.add_argument("--fix_ex", type=float, help="Fixed excision rate: fix_ex * genome_size * generations.")
    parser.add_argument("--sel_coeff", type=float, default=0.0, help="Selection coefficient for gene-disrupting TEs.")
    parser.add_argument("--euch_het_buffer", type=int, default=0, help="Buffer (bp) around genes for euchromatin.")
    parser.add_argument("--euch_het_bias", type=float, default=1.0, help="Bias factor for euchromatic excision.")
    parser.add_argument("-m", "--max-chrom", type=int, default=1, help="Max number of chromosomes to process in parallel.")
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    # Load data
    genome_records = parse_fasta(args.genome)
    genome_size = sum(len(rec.seq) for rec in genome_records.values())
    entries = parse_bed(args.bed)

    # Gene weights
    unique_genes = {e.feature_id for e in entries if e.feature_id.startswith("gene")}
    gene_weights = {}
    sel_file = "./gene_selection.tsv"
    if os.path.exists(sel_file):
        with open(sel_file) as f:
            for line in f:
                g, w = line.strip().split("\t")
                gene_weights[g] = float(w)
    else:
        if args.fix_ex is None:
            mu = args.sigma ** 2
            for g in sorted(unique_genes):
                gene_weights[g] = random.lognormvariate(mu, args.sigma)
            with open(sel_file, "w") as f:
                for g, w in gene_weights.items():
                    f.write(f"{g}\t{w:.4f}\n")

    # Figures
    if not args.no_fig and args.fix_ex is None:
        plot_lognormal(args.sigma, "lognormal_distribution.pdf")

    # Classification and nested groups
    entries, nest_groups = classify_entries(entries)

    # Weighted candidate curve
    eligible_entries = [e for e in entries if e.subtype in [
        "NON_NEST_GROUP_TE", "CUT_PAIR_TE_1", "CUT_PAIR_TE_2",
        "NEST_TE_IN_TE", "NEST_TE_IN_GENE"
    ]]
    if eligible_entries and not args.no_fig:
        lengths = [e.length() for e in eligible_entries]
        L95 = float(np.percentile(lengths, 95))
        plot_weighted_candidate_curve(args.k, L95, "weighted_candidate_selection.pdf")

    # Total excision count
    if args.fix_ex is not None:
        lambda_val = args.fix_ex * genome_size * args.generations
        excision_count = np.random.poisson(lambda_val)
    else:
        excision_count = calculate_excision_count(entries, args.rate, args.generations, gene_weights)

    print(f"Calculated number of TE excisions: {excision_count}")

    # Chromatin intervals
    euch_intervals = None
    if args.fix_ex is None and args.euch_het_buffer > 0:
        euch_intervals = build_euchromatin_intervals(entries, args.euch_het_buffer)

    # Decide parallel vs sequential
    chroms = sorted(genome_records.keys())
    if args.max_chrom > 1 and len(chroms) > 1:
        # Split entries and groups by chromosome
        entries_by_chrom = defaultdict(list)
        for e in entries:
            entries_by_chrom[e.chrom].append(e)
        nest_by_chrom = defaultdict(dict)
        for gid, grp in nest_groups.items():
            c = grp[0].chrom
            nest_by_chrom[c][gid] = grp

        # Count eligible per chrom
        elig_counts = {c: sum(1 for e in entries_by_chrom[c] if e.subtype in [
            "NON_NEST_GROUP_TE", "CUT_PAIR_TE_1", "CUT_PAIR_TE_2",
            "NEST_TE_IN_TE", "NEST_TE_IN_GENE"
        ]) for c in chroms}
        total_elig = sum(elig_counts.values()) or 1

        # Allocate per-chrom excisions
        exact = {c: excision_count * elig_counts[c] / total_elig for c in chroms}
        floors = {c: math.floor(exact[c]) for c in chroms}
        rem = excision_count - sum(floors.values())
        fracs = sorted(chroms, key=lambda c: exact[c] - floors[c], reverse=True)
        exc_per_chrom = floors.copy()
        for c in fracs[:rem]:
            exc_per_chrom[c] += 1

        # Prepare tasks
        tasks = []
        for idx, c in enumerate(chroms):
            tasks.append((
                c,
                entries_by_chrom[c],
                genome_records[c],
                nest_by_chrom.get(c, {}),
                exc_per_chrom[c],
                args.seed + idx,
                args.k,
                gene_weights,
                args.generations,
                args.sel_coeff,
                euch_intervals,
                args.euch_het_bias,
                args.soloLTR_freq
            ))

        # Run in parallel
        updated_genome = {}
        updated_entries = []
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.max_chrom) as exe:
            for ug, ue in exe.map(process_chrom, tasks):
                updated_genome.update(ug)
                updated_entries.extend(ue)
    else:
        # Single-threaded path
        removals = select_removals(
            entries, nest_groups, excision_count, args.seed,
            args.k, gene_weights, args.generations, args.sel_coeff,
            euch_intervals=euch_intervals, euch_het_bias=args.euch_het_bias
        )
        random.seed(args.seed + 1)
        updated_genome, updated_entries = simulate_excision(
            genome_records, entries, nest_groups, removals, args.soloLTR_freq
        )

    # Failsafe consolidation and output
    final_bed = fail_safe_consolidation(updated_entries)
    write_fasta(updated_genome, args.output)
    write_bed(final_bed, args.output)

if __name__ == "__main__":
    main()
