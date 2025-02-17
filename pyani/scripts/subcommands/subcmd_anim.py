#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# (c) The James Hutton Institute 2017-2019
# (c) University of Strathclyde 2019-2024
# Author: Leighton Pritchard
#
# Contact:
# leighton.pritchard@strath.ac.uk
#
# Leighton Pritchard,
# Strathclyde Institute for Pharmacy and Biomedical Sciences,
# 161 Cathedral Street,
# Glasgow,
# G4 0RE
# Scotland,
# UK
#
# The MIT License
#
# Copyright (c) 2017-2019 The James Hutton Institute
# Copyright (c) 2019-2024 University of Strathclyde
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""Provides the anim subcommand for pyani."""

import datetime
import logging

from argparse import Namespace
from itertools import permutations
from pathlib import Path
from typing import Any, List, NamedTuple, Optional, Tuple

from tqdm import tqdm

from pyani import (
    PyaniException,
    anim,
    pyani_config,
    pyani_jobs,
    run_sge,
    run_multiprocessing as run_mp,
)
from pyani.pyani_files import collect_existing_output
from pyani.pyani_orm import (
    Comparison,
    PyaniORMException,
    add_run,
    add_run_genomes,
    filter_existing_comparisons,
    get_session,
    update_comparison_matrices,
)
from pyani.pyani_tools import termcolor


# Convenience struct describing a pairwise comparison job for the SQLAlchemy
# implementation
class ComparisonJob(NamedTuple):
    """Pairwise comparison job for the SQLAlchemy implementation."""

    query: Any  # TODO: replace with appropriate type
    subject: Any  # TODO: replace with appropriate type
    filtercmd: str
    nucmercmd: str
    outfile: Path
    job: Optional[pyani_jobs.Job]


# Convenience struct describing an analysis run
class RunData(NamedTuple):
    """Convenience struct describing an analysis run."""

    method: str
    name: str
    date: datetime.datetime
    cmdline: str


class ComparisonResult(NamedTuple):
    """Convenience struct for a single nucmer comparison result."""

    qid: float
    sid: float
    aln_length: int
    sim_errs: int
    pid: float
    qlen: int
    slen: int
    qcov: float
    scov: float


class ProgData(NamedTuple):
    """Convenience struct for comparison program data/info."""

    program: str
    version: str


class ProgParams(NamedTuple):
    """Convenience struct for comparison parameters.

    Use default of zero for fragsize or else db queries will not work
    as SQLite/Python nulls do not match up well
    """

    fragsize: str
    maxmatch: bool


def subcmd_anim(args: Namespace) -> None:
    """Perform ANIm on all genome files in an input directory.

    :param args:  Namespace, command-line arguments

    Finds ANI by the ANIm method, as described in Richter et al (2009)
    Proc Natl Acad Sci USA 106: 19126-19131 doi:10.1073/pnas.0906412106.

    All FASTA format files (selected by suffix) in the input directory
    are compared against each other, pairwise, using NUCmer (whose path must
    be provided).

    For each pairwise comparison, the NUCmer .delta file output is parsed to
    obtain an alignment length and similarity error count for every unique
    region alignment between the two organisms, as represented by
    sequences in the FASTA files. These are processed to calculated aligned
    sequence lengths, average nucleotide identity (ANI) percentages, coverage
    (aligned percentage of whole genome - forward direction), and similarity
    error count for each pairwise comparison.

    The calculated values are deposited in the SQLite3 database being used for
    the analysis.

    For each pairwise comparison the NUCmer output is stored in the output
    directory for long enough to extract summary information, but for each run
    the output is gzip compressed. Once all runs are complete, the outputs
    for each comparison are concatenated into a single gzip archive.
    """
    # Create logger
    logger = logging.getLogger(__name__)

    # Announce the analysis
    logger.info(termcolor("Running ANIm analysis", bold=True))

    # Get current nucmer version
    nucmer_version = anim.get_version(args.nucmer_exe)
    logger.info(termcolor("MUMMer nucmer version: %s", "cyan"), nucmer_version)

    # Use the provided name or make one for the analysis
    start_time = datetime.datetime.now()
    name = args.name or "_".join(["ANIm", start_time.isoformat()])
    logger.info(termcolor("Analysis name: %s", "cyan"), name)

    # Get connection to existing database. This may or may not have data
    logger.debug("Connecting to database %s", args.dbpath)
    try:
        session = get_session(args.dbpath)
    except Exception as exc:
        logger.error(
            "Could not connect to database %s (exiting)", args.dbpath, exc_info=True
        )
        raise SystemExit(1) from exc

    # Add information about this run to the database
    logger.debug("Adding run info to database %s...", args.dbpath)
    try:
        run, run_id = add_run(
            session,
            method="ANIm",
            cmdline=args.cmdline,
            date=start_time,
            status="started",
            name=name,
        )
    except PyaniORMException as exc:
        logger.error("Could not add run to the database (exiting)", exc_info=True)
        raise SystemExit(1) from exc
    logger.debug("...added run ID: %s to the database", run_id)

    # Identify input files for comparison, and populate the database
    logger.debug("Adding genomes for run %s to database...", run_id)
    try:
        genome_ids = add_run_genomes(
            session, run, args.indir, args.classes, args.labels
        )
    except PyaniORMException as exc:
        logger.error("Could not add genomes to database for run %s (exiting)", run_id)
        raise SystemExit(1) from exc
    logger.debug("\t...added genome IDs: %s", genome_ids)

    # Generate commandlines for NUCmer analysis and output compression
    logger.info("Generating ANIm command-lines")
    deltadir = args.outdir / pyani_config.ALIGNDIR["ANIm"]
    logger.debug("NUCmer output will be written temporarily to %s", deltadir)

    # Create output directories
    logger.debug("Creating output directory %s", deltadir)
    try:
        deltadir.mkdir(exist_ok=True, parents=True)
    except IOError as exc:
        logger.error(
            "Could not create output directory %s (exiting)", deltadir, exc_info=True
        )
        raise SystemError(1) from exc

    # Get list of genome IDs for this analysis from the database
    logger.info("Compiling genomes for comparison")
    genomes = run.genomes.all()
    logger.debug("Collected %s genomes for this run", len(genomes))

    # Generate all pair combinations of genome IDs as a list of (Genome, Genome) tuples
    logger.info(
        "Compiling pairwise comparisons (this can take time for large datasets)..."
    )
    comparisons = list(permutations(tqdm(genomes, disable=args.disable_tqdm), 2))
    logger.info("\t...total pairwise comparisons to be performed: %s", len(comparisons))

    # Check for existing comparisons; if one has been done (for the same
    # software package, version, and setting) we add the comparison to this run,
    # but remove it from the list of comparisons to be performed
    logger.info("Checking database for existing comparison data...")
    comparisons_to_run = filter_existing_comparisons(
        session,
        run,
        comparisons,
        "nucmer",
        nucmer_version,
        fragsize=None,
        maxmatch=args.maxmatch,
    )
    logger.info(
        "\t...after check, still need to run %s comparisons", len(comparisons_to_run)
    )

    # If there are no comparisons to run, update the Run matrices and exit
    # from this function
    if not comparisons_to_run:
        logger.info(
            termcolor(
                "All comparison results present in database (skipping comparisons)",
                "magenta",
            )
        )
        logger.info("Updating summary matrices with existing results")
        update_comparison_matrices(session, run)
        return

    # If we are in recovery mode, we are salvaging output from a previous
    # run, and do not necessarily need to rerun all the jobs. In this case,
    # we prepare a list of output files we want to recover from the results
    # in the output directory.
    if args.recovery:
        logger.warning("Entering recovery mode")
        logger.debug(
            "\tIn this mode, existing comparison output from %s is reused", deltadir
        )
        existingfiles = collect_existing_output(deltadir, "nucmer", args)
        if existingfiles:
            logger.info(
                "Recover mode identified %s existing output files for reuse: %s (etc)",
                len(existingfiles),
                existingfiles[0],
            )
        else:
            logger.info("Recovery mode identified no existing output files")
    else:
        existingfiles = list()
        logger.debug("\tAssuming no pre-existing output files")

    # Create list of NUCmer jobs for each comparison still to be performed
    logger.info("Creating NUCmer jobs for ANIm")
    joblist = generate_joblist(comparisons_to_run, existingfiles, args)
    logger.debug(
        "Generated %s jobs, %s comparisons", len(joblist), len(comparisons_to_run)
    )

    # Pass jobs to appropriate scheduler
    logger.debug("Passing %s jobs to %s...", len(joblist), args.scheduler)
    run_anim_jobs(joblist, args)
    logger.info("...jobs complete")

    # Process output and add results to database
    # This requires us to drop out of threading/multiprocessing: Python's SQLite3
    # interface doesn't allow sharing connections and cursors
    logger.info("Adding comparison results to database...")
    update_comparison_results(joblist, run, session, nucmer_version, args)
    update_comparison_matrices(session, run)
    logger.info("...database updated.")


def generate_joblist(
    comparisons: List[Tuple], existingfiles: List[Path], args: Namespace
) -> List[ComparisonJob]:
    """Return list of ComparisonJobs.

    :param comparisons:  list of (Genome, Genome) tuples
    :param existingfiles:  list of pre-existing nucmer output files
    :param args:  Namespace of command-line arguments for the run
    """
    logger = logging.getLogger(__name__)

    existingfileset = set(existingfiles)  # Path objects hashable

    joblist = []  # will hold ComparisonJob structs
    jobs = {"new": 0, "old": 0}  # will hold counts of new/old jobs for reporting
    for idx, (subject, query) in enumerate(
        tqdm(comparisons, disable=args.disable_tqdm)
    ):
        ncmd, dcmd = anim.construct_nucmer_cmdline(
            query.path,
            subject.path,
            args.outdir,
            args.nucmer_exe,
            args.filter_exe,
            args.maxmatch,
        )

        logger.debug("Commands to run:\n\t%s, %s", query, subject)
        # Having created forward and reverse commandlines, we need to
        # run the same operations on both
        for ncmd, dcmd, qinfo, sinfo in [(ncmd, dcmd, query, subject)]:
            logger.debug("Commands to run:\n\t%s\n\t%s", ncmd, dcmd)
            outprefix = ncmd.split()[3]  # prefix for NUCmer output
            if args.nofilter:
                outfname = Path(outprefix + ".delta")
            else:
                outfname = Path(outprefix + ".filter")
            logger.debug("Expected output file for db: %s", str(outfname))

            # If we're in recovery mode, we don't want to repeat a computational
            # comparison that already exists, so we check whether the ultimate
            # output is in the set of existing files and, if not, we add the jobs

            # The comparisons collections always gets updated, so that results are
            # added to the database whether they come from recovery mode or are run
            # in this call of the script.
            if args.recovery and outfname in existingfileset:
                logger.debug("Recovering output from %s, not submitting job", outfname)
                # Need to track the expected output, but set the job itself to None:
                joblist.append(ComparisonJob(qinfo, sinfo, dcmd, ncmd, outfname, None))
                jobs["old"] += 1
            else:
                logger.debug("Building job")
                # Build jobs
                njob = pyani_jobs.Job(f"{args.jobprefix}_{idx:06d}-n", ncmd)
                fjob = pyani_jobs.Job(f"{args.jobprefix}_{idx:06d}-f", dcmd)
                fjob.add_dependency(njob)
                joblist.append(ComparisonJob(qinfo, sinfo, dcmd, ncmd, outfname, fjob))
                jobs["new"] += 1
    logger.info(
        "Results not found for %d comparisons; %d new jobs built.",
        jobs["new"],
        jobs["new"],
    )
    if existingfileset:
        logger.info("Retrieving results for %d previous comparisons.", jobs["old"])

    return joblist


def run_anim_jobs(joblist: List[ComparisonJob], args: Namespace) -> None:
    """Pass ANIm nucmer jobs to the scheduler.

    :param joblist:           list of ComparisonJob namedtuples
    :param args:              command-line arguments for the run
    """
    logger = logging.getLogger(__name__)
    logger.debug("Scheduler: %s", args.scheduler)

    # Entries with None seen in recovery mode:
    jobs = [_.job for _ in joblist if _.job]

    if args.scheduler == "multiprocessing":
        logger.info("Running jobs with multiprocessing")
        if not args.workers:
            logger.debug("(using maximum number of worker threads)")
        else:
            logger.debug("(using %d worker threads, if available)", args.workers)
        cumval = run_mp.run_dependency_graph(jobs, workers=args.workers)
        if cumval > 0:
            logger.error(
                "At least one NUCmer comparison failed. Please investigate (exiting)"
            )
            raise PyaniException("Multiprocessing run failed in ANIm")
        logger.info("Multiprocessing run completed without error")
    elif args.scheduler.lower() == "sge":
        logger.info("Running jobs with SGE")
        logger.debug("Setting jobarray group size to %d", args.sgegroupsize)
        logger.debug("Joblist contains %d jobs", len(joblist))
        run_sge.run_dependency_graph(
            jobs,
            jgprefix=args.jobprefix,
            sgegroupsize=args.sgegroupsize,
            sgeargs=args.sgeargs,
        )
    else:
        logger.error(termcolor("Scheduler %s not recognised", "red"), args.scheduler)
        raise SystemError(1)


def update_comparison_results(
    joblist: List[ComparisonJob], run, session, nucmer_version: str, args: Namespace
) -> None:
    """Update the Comparison table with the completed result set.

    :param joblist:         list of ComparisonJob namedtuples
    :param run:             Run ORM object for the current ANIm run
    :param session:         active pyanidb session via ORM
    :param nucmer_version:  version of nucmer used for the comparison
    :param args:            command-line arguments for this run

    The Comparison table stores individual comparison results, one per row.
    """
    logger = logging.getLogger(__name__)

    # Add individual results to Comparison table
    for job in tqdm(joblist, disable=args.disable_tqdm):
        logger.debug(
            "\t%s vs %s; job %s", job.query.description, job.subject.description, job
        )
        qaln_length, saln_length, pc_id, sim_errs = anim.parse_delta(job.outfile)
        qcov = qaln_length / job.query.length
        scov = saln_length / job.subject.length
        run.comparisons.append(
            Comparison(
                query=job.query,
                subject=job.subject,
                aln_length=qaln_length,
                sim_errs=sim_errs,
                identity=pc_id,
                cov_query=qcov,
                cov_subject=scov,
                program="nucmer",
                version=nucmer_version,
                fragsize=None,
                maxmatch=args.maxmatch,
            )
        )

    # Populate db
    logger.debug("Committing results to database")
    session.commit()
